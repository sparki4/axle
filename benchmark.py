"""Compare AXLE with equivalent handwritten Rust; includes process startup overhead."""
import argparse
import json
import pathlib
import platform
import statistics
import subprocess
import tempfile
import time
import axle

BASELINE = r'''
fn mix(n: u64) -> u64 {
    let mut i = 0u64;
    let mut state = 1u64;
    while i < n {
        state = (state ^ i).wrapping_mul(6364136223846793005u64).wrapping_add(1442695040888963407u64);
        i = i.wrapping_add(1);
    }
    state
}
fn main() {
    let n = std::env::args().nth(1).unwrap().parse::<u64>().unwrap();
    println!("{}", mix(n));
}
'''

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=50000000)
    parser.add_argument('--samples', type=int, default=7)
    parser.add_argument('--output', type=pathlib.Path)
    args = parser.parse_args()
    if args.iterations < 1 or args.iterations > 2**64 - 1 or args.samples < 3:
        parser.error('iterations must be a positive u64; samples must be at least 3')
    root = pathlib.Path(__file__).resolve().parent
    start = time.perf_counter()
    rust = axle.compile_source((root / 'examples/hash.axl').read_text())
    frontend_ms = (time.perf_counter() - start) * 1000
    with tempfile.TemporaryDirectory(prefix='axle-bench-') as directory:
        build_seconds, executables = {}, {}
        for name, source in [('axle', rust), ('rust', BASELINE)]:
            start = time.perf_counter()
            executables[name] = axle.build(source, pathlib.Path(directory) / (name + '.exe'))
            build_seconds[name] = time.perf_counter() - start
        def run(name):
            start = time.perf_counter()
            result = subprocess.run([str(executables[name]), str(args.iterations)], capture_output=True, text=True, check=True)
            return (time.perf_counter() - start) * 1000, result.stdout.strip()
        warm = {name: run(name)[1] for name in executables}
        if warm['axle'] != warm['rust']: raise RuntimeError('result mismatch')
        # Independently verify a small workload with Python's exact integers.
        state = 1
        for i in range(1000): state = ((state ^ i) * 6364136223846793005 + 1442695040888963407) & (2**64 - 1)
        for name, exe in executables.items():
            result = subprocess.run([str(exe), '1000'], capture_output=True, text=True, check=True)
            if result.stdout.strip() != str(state): raise RuntimeError(name + ' failed reference check')
        times = {name: [] for name in executables}
        for sample in range(args.samples):
            order = ['axle', 'rust'] if sample % 2 == 0 else ['rust', 'axle']
            for name in order:
                elapsed, output = run(name)
                if output != warm[name]: raise RuntimeError('unstable result')
                times[name].append(elapsed)
        medians = {name: statistics.median(values) for name, values in times.items()}
        report = {
            'platform': platform.platform(), 'python': platform.python_version(),
            'rustc': subprocess.check_output(['rustc', '--version'], text=True).strip(),
            'iterations': args.iterations, 'samples': args.samples, 'result': warm['axle'],
            'frontend_ms': frontend_ms, 'native_build_seconds': build_seconds,
            'median_ms': medians, 'axle_over_rust': medians['axle'] / medians['rust'], 'all_samples_ms': times,
            'scope': 'One integer recurrence workload. Wall clock includes process startup and printing. Same rustc flags. No general speed or compile-time claim.'
        }
    rendered = json.dumps(report, indent=2)
    print(rendered)
    if args.output: args.output.write_text(rendered + '\n', encoding='utf-8')

if __name__ == '__main__': main()
