#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$(mktemp -d "$root/artifacts/quest/file-audio-protocol-XXXXXXXX")"
printf '%s\n' "$out"
c++ -std=c++17 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$root/third_party/nightfall/addons/nightfall-stream/src" \
  "$root/scripts/quest/test_file_audio_protocol.cpp" -o "$out/protocol-test"
ASAN_OPTIONS=detect_leaks=1 "$out/protocol-test" 2>&1 | tee "$out/test.log"
c++ -std=c++17 -O1 -g -Wall -Wextra -Werror -fno-exceptions -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"$root/third_party/nightfall/addons/nightfall-stream/src" \
  "$root/scripts/quest/test_file_audio_protocol.cpp" -o "$out/protocol-no-exceptions-test"
ASAN_OPTIONS=detect_leaks=1 "$out/protocol-no-exceptions-test" 2>&1 | tee "$out/no-exceptions.log"
sha256sum "$root/third_party/nightfall/addons/nightfall-stream/src/audio/file_audio_protocol.h" \
  "$root/scripts/quest/test_file_audio_protocol.cpp" "$out/protocol-test" > "$out/SHA256SUMS"
if [[ $# == 1 ]]; then
  vectors="$1"
  for name in first seek; do
    if [[ "$name" == first ]]; then epoch=7; generation=9; else epoch=8; generation=10; fi
    ASAN_OPTIONS=detect_leaks=1 "$out/protocol-test" "$vectors/$name.wire" "$out/$name-decoded.pcm" \
      "$epoch" "$generation" 2>&1 | tee "$out/$name.log"
    cmp "$vectors/$name.pcm" "$out/$name-decoded.pcm"
  done
  python3 - "$vectors" "$out" <<'PY'
import hashlib, json, sys
from pathlib import Path
vectors, out = map(Path, sys.argv[1:])
hash_of = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
manifest = json.loads((vectors / 'manifest.json').read_text())
results = []
for case in manifest['cases']:
    name = case['name']
    expected = vectors / (name + '.pcm')
    actual = out / (name + '-decoded.pcm')
    assert actual.read_bytes() == expected.read_bytes()
    results.append(dict(name=name, frames=case['frames'], exact_pcm_bytes=True,
                        sha256=hash_of(actual), wire_sha256=hash_of(vectors / (name + '.wire'))))
(out / 'file-verification.json').write_text(json.dumps(dict(passed=True, scope='Actual file decode and cross-language wire; no TLS/device/Quest',
    input_manifest_sha256=hash_of(vectors / 'manifest.json'), cases=results), indent=2) + '\n')
print('PASS actual-file Python-to-C++ exact PCM bytes, first and seek/partial EOF')
PY
fi
