#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$QUEST_CACHE/identity-tests"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread -fsanitize=address,undefined \
  -I"$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src/video" \
  "$QUEST_SCRIPTS/test_frame_identity.cpp" -o "$QUEST_CACHE/identity-tests/frame-identity"
"$QUEST_CACHE/identity-tests/frame-identity"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread -fsanitize=address,undefined \
  -I"$QUEST_PROJECT/third_party/nightfall/extensions/nightfall-xr/include" \
  "$QUEST_SCRIPTS/test_submission_identity.cpp" -o "$QUEST_CACHE/identity-tests/submission-identity"
"$QUEST_CACHE/identity-tests/submission-identity"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread -fsanitize=address,undefined \
  -I"$QUEST_PROJECT/third_party/nightfall/extensions/nightfall-xr/include" \
  "$QUEST_SCRIPTS/test_pointer_geometry.cpp" -o "$QUEST_CACHE/identity-tests/pointer-geometry"
"$QUEST_CACHE/identity-tests/pointer-geometry"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -pthread -fsanitize=address,undefined \
  -I"$QUEST_PROJECT/third_party/nightfall/extensions/nightfall-xr/include" \
  "$QUEST_SCRIPTS/test_pointer_ticket.cpp" -o "$QUEST_CACHE/identity-tests/pointer-ticket"
"$QUEST_CACHE/identity-tests/pointer-ticket"
g++ -std=c++17 -O1 -g -Wall -Wextra -Werror -fsanitize=address,undefined \
  -I"$QUEST_PROJECT/third_party/nightfall/addons/nightfall-stream/src/config" \
  "$QUEST_SCRIPTS/test_pointer_input_gate.cpp" -o "$QUEST_CACHE/identity-tests/pointer-input-gate"
"$QUEST_CACHE/identity-tests/pointer-input-gate"
