"""Rust and worker-side source for AXLE extern bridges ("py:" and "js:").

A bridge starts one persistent worker process per foreign module and speaks a
one-line text protocol over its stdin/stdout.  Replies are prefixed with the
control byte 0x01 so the foreign code's own prints pass through untouched.
"""

PY_SERVER = r'''import sys, os, importlib, importlib.util
target, root = sys.argv[1], sys.argv[2]
out = sys.__stdout__
def reply(text):
    out.write("\x01AXLE " + text + "\n")
    out.flush()
mod, failure = None, None
try:
    sys.path.insert(0, root)
    if os.path.isabs(target):
        sys.path.insert(0, os.path.dirname(target))
        name = os.path.splitext(os.path.basename(target))[0]
        spec = importlib.util.spec_from_file_location(name, target)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    else:
        mod = importlib.import_module(target)
except BaseException as error:
    failure = "cannot load %s: %s: %s" % (target, type(error).__name__, error)
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    try:
        if failure:
            raise RuntimeError(failure)
        function = getattr(mod, parts[0], None)
        if not callable(function):
            raise AttributeError("no callable named %s in %s" % (parts[0], target))
        def convert(a):
            if a.startswith("s:"): return bytes.fromhex(a[2:]).decode("utf-8")
            if a == "true": return True
            if a == "false": return False
            try: return int(a)
            except ValueError: return float(a)
        args = [convert(a) for a in parts[1:]]
        result = function(*args)
        if result is None:
            reply("ok unit")
        elif isinstance(result, bool):
            reply("ok " + ("true" if result else "false"))
        elif isinstance(result, int):
            reply("ok %d" % result)
        elif isinstance(result, str):
            reply("ok s:" + result.encode("utf-8").hex())
        elif isinstance(result, float):
            reply("ok " + repr(result))
        else:
            reply("err unsupported return type %s (bridges carry i64, u64, f64, str, bool and unit)" % type(result).__name__)
    except BaseException as error:
        reply("err %s: %s" % (type(error).__name__, str(error).replace("\n", " ")))
'''

JS_SERVER = r'''const path = require("path");
const { pathToFileURL } = require("url");
const [target, root] = process.argv.slice(-2);
function reply(text) { process.stdout.write("\u0001AXLE " + text + "\n"); }
let mod = null, failure = null;
const ready = (async () => {
  try {
    if (path.isAbsolute(target)) {
      mod = target.endsWith(".mjs") ? await import(pathToFileURL(target).href) : require(target);
    } else {
      mod = require(require.resolve(target, { paths: [root] }));
    }
  } catch (error) { failure = "cannot load " + target + ": " + ((error && error.message) || error); }
})();
function parse(a) {
  if (a.startsWith("s:")) return Buffer.from(a.slice(2), "hex").toString("utf8");
  if (a === "true") return true;
  if (a === "false") return false;
  if (/[.eEnNiI]/.test(a) && !/^-?\d+$/.test(a)) return Number(a);
  const v = BigInt(a);
  return (v >= -9007199254740991n && v <= 9007199254740991n) ? Number(v) : v;
}
async function handle(line) {
  const parts = line.trim().split(/\s+/);
  if (!parts[0]) return;
  try {
    await ready;
    if (failure) throw new Error(failure);
    const fn = mod[parts[0]] ?? (mod.default && mod.default[parts[0]]);
    if (typeof fn !== "function") throw new Error("no function named " + parts[0] + " in " + target);
    const result = await fn(...parts.slice(1).map(parse));
    if (result === undefined || result === null) reply("ok unit");
    else if (typeof result === "boolean" || typeof result === "bigint") reply("ok " + result);
    else if (typeof result === "number") reply("ok " + result);
    else if (typeof result === "string") reply("ok s:" + Buffer.from(result, "utf8").toString("hex"));
    else reply("err unsupported return value " + String(result) + " (bridges carry i64, u64, f64, str, bool and unit)");
  } catch (error) { reply("err " + String((error && error.message) || error).replace(/\n/g, " ")); }
}
let chain = Promise.resolve();
require("readline").createInterface({ input: process.stdin }).on("line", (line) => { chain = chain.then(() => handle(line)); });
'''

RUST_RUNTIME = r'''
struct AxleWorker { key: String, child: std::process::Child, stdin: std::process::ChildStdin, stdout: std::io::BufReader<std::process::ChildStdout> }
thread_local! { static AXLE_WORKERS: std::cell::RefCell<Vec<AxleWorker>> = std::cell::RefCell::new(Vec::new()); }
const AXLE_PY_SERVER: &str = r####"@PY@"####;
const AXLE_JS_SERVER: &str = r####"@JS@"####;
fn axle_hex_arg(text: &str) -> String {
    let mut out = String::from("s:");
    for byte in text.as_bytes() { out.push_str(&format!("{:02x}", byte)); }
    out
}
fn axle_hex_decode(reply: &str) -> Option<String> {
    let digits = reply.strip_prefix("s:")?;
    if digits.len() % 2 != 0 { return None; }
    let mut bytes = Vec::with_capacity(digits.len() / 2);
    for i in (0..digits.len()).step_by(2) { bytes.push(u8::from_str_radix(digits.get(i..i + 2)?, 16).ok()?); }
    String::from_utf8(bytes).ok()
}
fn axle_bridge_fail(function: &str, what: &str, detail: &str) -> ! {
    panic!("axle bridge error in {}: {} {}", function, what, detail)
}
fn axle_spawn(lang: &str, target: &str, root: &str) -> Result<AxleWorker, String> {
    use std::process::{Command, Stdio};
    let mut candidates: Vec<String> = Vec::new();
    if lang == "py" {
        if let Ok(p) = std::env::var("AXLE_PYTHON") { candidates.push(p); }
        if cfg!(windows) { candidates.push("python".into()); candidates.push("py".into()); }
        else { candidates.push("python3".into()); candidates.push("python".into()); }
    } else {
        if let Ok(p) = std::env::var("AXLE_NODE") { candidates.push(p); }
        candidates.push("node".into());
    }
    let mut last = String::from("no candidates");
    for program in candidates {
        let mut command = Command::new(&program);
        if lang == "py" { command.args(["-u", "-c", AXLE_PY_SERVER, target, root]); }
        else { command.args(["-e", AXLE_JS_SERVER, "--", target, root]); }
        command.stdin(Stdio::piped()).stdout(Stdio::piped());
        match command.spawn() {
            Ok(mut child) => {
                let stdin = child.stdin.take().expect("worker stdin");
                let stdout = std::io::BufReader::new(child.stdout.take().expect("worker stdout"));
                return Ok(AxleWorker { key: format!("{}:{}:{}", lang, target, root), child, stdin, stdout });
            }
            Err(error) => last = format!("{}: {}", program, error),
        }
    }
    Err(format!("cannot start the {} interpreter ({}); set AXLE_PYTHON or AXLE_NODE to its path", lang, last))
}
fn axle_bridge_call(lang: &str, target: &str, root: &str, function: &str, args: &[String]) -> String {
    use std::io::{BufRead, Write};
    let key = format!("{}:{}:{}", lang, target, root);
    AXLE_WORKERS.with(|cell| {
        let mut workers = cell.borrow_mut();
        let index = match workers.iter().position(|w| w.key == key) {
            Some(i) => i,
            None => match axle_spawn(lang, target, root) {
                Ok(worker) => { workers.push(worker); workers.len() - 1 }
                Err(error) => axle_bridge_fail(function, "", &error),
            },
        };
        let worker = &mut workers[index];
        let mut request = String::from(function);
        for a in args { request.push(' '); request.push_str(a); }
        request.push('\n');
        if worker.stdin.write_all(request.as_bytes()).and_then(|_| worker.stdin.flush()).is_err() {
            axle_bridge_fail(function, "the foreign worker is not running", "");
        }
        let mut line = String::new();
        loop {
            line.clear();
            match worker.stdout.read_line(&mut line) {
                Ok(0) | Err(_) => axle_bridge_fail(function, "the foreign worker exited before replying", ""),
                Ok(_) => {}
            }
            let text = line.trim_end_matches(&['\r', '\n'][..]);
            if let Some(rest) = text.strip_prefix("\u{1}AXLE ") {
                if let Some(value) = rest.strip_prefix("ok ") { return value.to_string(); }
                if let Some(message) = rest.strip_prefix("err ") { axle_bridge_fail(function, "foreign call failed:", message); }
                axle_bridge_fail(function, "malformed reply", rest);
            }
            println!("{}", text);
        }
    })
}
'''

def rust_runtime():
    return RUST_RUNTIME.replace('@PY@', PY_SERVER).replace('@JS@', JS_SERVER)
