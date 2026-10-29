"""Example: add Ruby as a host as well as a guest. Requires installed Ruby."""
import json
import shutil
from polyglot import Host, checked

def build(source, output, profile, include):
    checked(['ruby', '-c', str(source)])
    if source != output: shutil.copyfile(source, output)

def register(hosts, guests):
    hosts['ruby'] = Host(
        extension='.rb', lex='hash',
        emit_call=lambda argv, index: 'STDOUT.flush; STDERR.flush; abort("guest failed") unless system(*' + json.dumps(list(map(str, argv))) + ')',
        preamble='', builder=build, launcher=lambda path: ['ruby', str(path)],
    )
    guests['ruby'].host = 'ruby'
