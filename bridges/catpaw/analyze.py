import re

f = '/Applications/CatPawAI.app/Contents/Resources/app/extensions/mt-idekit.mt-idekit-code/out/extension.js'
data = open(f, encoding='utf-8', errors='replace').read()

needles = [
    'Kd=',
    'Kd[',
    'tenantId',
    'getTenant',
    'key1',
    'Lx=',
    'ENV_CONFIG_FILE',
    'isEncryptionEnabled',
    'encryptRequest(',
    'WT=',
    'wA=',
    'getValue(',
    'meituancatpaw',
    'gpt/openai/stream',
    'api/gpt/stream',
    'gpt/chat/completions',
]
import sys
ctx = int(sys.argv[1]) if len(sys.argv) > 1 else 500
for needle in needles:
    idxs = [m.start() for m in re.finditer(re.escape(needle), data)]
    print('===', needle, 'matches:', len(idxs))
    for i in idxs[:2]:
        print(repr(data[max(0, i-ctx):i+ctx]))
        print('---')
