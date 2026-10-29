"""Matched-profile runtime comparisons; raw samples and correctness checks included."""
import argparse
import json
import pathlib
import platform
import random
import statistics
import subprocess
import tempfile
import time

import axle
from benchmark import BASELINE as HASH_RUST

REDUCTION_RUST = r'''
fn main() {
    let n = std::env::args().nth(1).unwrap().parse::<u64>().unwrap();
    let rounds = std::env::args().nth(2).unwrap().parse::<u64>().unwrap();
    let data: Vec<u64> = (0..n).collect();
    let mut total = 0u64;
    for _ in 0..rounds {
        let sum = std::hint::black_box(&data).iter().fold(0u64, |a, &b| a.wrapping_add(b));
        total = total.wrapping_add(sum);
    }
    println!("{}", total);
}
'''

def hash_reference(count):
    state = 1
    for i in range(count):
        state = ((state ^ i) * 6364136223846793005 + 1442695040888963407) & (2**64-1)
    return str(state)

def run(executable, arguments):
    start = time.perf_counter()
    result = subprocess.run([str(executable), *map(str, arguments)], capture_output=True, text=True, check=True)
    return (time.perf_counter() - start) * 1000, result.stdout.strip()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=9)
    parser.add_argument('--output', type=pathlib.Path, default=pathlib.Path('benchmark-v02.json'))
    options = parser.parse_args()
    if options.samples < 3: parser.error('at least three samples required')
    root = pathlib.Path(__file__).resolve().parent
    workloads = {
        'integer_recurrence': {'file': 'hash.axl', 'rust': HASH_RUST, 'arguments': [50000000],
                               'check_args': [1000], 'check_result': hash_reference(1000)},
        'buffer_reduction': {'file': 'reduction.axl', 'rust': REDUCTION_RUST, 'arguments': [4000000, 32],
                             'check_args': [1000, 7], 'check_result': str(999*1000//2*7)},
    }
    report = {
        'axle_version': axle.VERSION, 'platform': platform.platform(), 'cpu': platform.processor(),
        'rustc': subprocess.check_output(['rustc', '-vV'], text=True).strip(),
        'samples': options.samples, 'workloads': {},
        'method': 'Each profile uses the same rustc flags for AXLE and Rust. Randomized interleaving with fixed seed, one warmup, subprocess wall time including startup, allocation and output. black_box prevents repeated reductions from being hoisted. No general superiority claim.',
        'semantics': 'Integer workloads use identical wrapping arithmetic. The reduction Rust baseline uses an owned Vec; AXLE uses a shared Rc<RefCell<Vec>> buffer. No aliasing is exercised in this workload. AXLE pays for its additional shared-handle semantics.',
    }
    with tempfile.TemporaryDirectory(prefix='axle-suite-') as directory:
        for workload, specification in workloads.items():
            rust = axle.compile_file(root/'examples'/specification['file'])
            entries, times, builds = {}, {}, {}
            for profile in ['release', 'native']:
                for language, source in [('axle', rust), ('rust', specification['rust'])]:
                    key = language + '_' + profile
                    start = time.perf_counter()
                    exe = axle.build(source, pathlib.Path(directory)/(workload+'_'+key+'.exe'), profile)
                    builds[key] = time.perf_counter() - start
                    _, check = run(exe, specification['check_args'])
                    if check != specification['check_result']: raise RuntimeError(key + ' failed Python reference')
                    entries[key] = exe
                    times[key] = []
            warmups = {key: run(exe, specification['arguments'])[1] for key, exe in entries.items()}
            if len(set(warmups.values())) != 1: raise RuntimeError(workload + ' output mismatch')
            if workload == 'buffer_reduction':
                n, rounds = specification['arguments']
                if warmups['axle_release'] != str((n*(n-1)//2*rounds) & (2**64-1)):
                    raise RuntimeError('measured reduction failed independent reference')
            rng = random.Random(813)
            for _ in range(options.samples):
                order = list(entries)
                rng.shuffle(order)
                for key in order:
                    elapsed, answer = run(entries[key], specification['arguments'])
                    if answer != warmups[key]: raise RuntimeError('unstable output ' + key)
                    times[key].append(elapsed)
            medians = {key: statistics.median(values) for key, values in times.items()}
            report['workloads'][workload] = {
                'arguments': specification['arguments'], 'result': warmups['axle_release'],
                'median_ms': medians,
                'axle_over_rust': {profile: medians['axle_'+profile]/medians['rust_'+profile] for profile in ['release', 'native']},
                'all_samples_ms': times, 'build_seconds': builds,
            }
    options.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    for name, result in report['workloads'].items():
        print(name + ': ' + json.dumps(result['median_ms']))
        print('AXLE/Rust ratios: ' + json.dumps(result['axle_over_rust']))
    print('Full report: ' + str(options.output))

if __name__ == '__main__': main()
