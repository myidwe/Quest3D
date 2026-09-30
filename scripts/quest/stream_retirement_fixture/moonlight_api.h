#pragma once
#include <godot_cpp/classes/mutex.hpp>
#include "network/http_requester.h"
// The isolated 4.7 API generator qualifies this existing engine class.
// Keep the production TextureUploader header unchanged in this fixture.
namespace godot { using Mutex = CoreBind::Mutex; }
// Production Android bindings predate godot-cpp's RefCounted memnew return
// change. Keep this non-target, manually owned HTTP companion on that API.
namespace godot {
template<> struct memnew_result<HttpRequester> {
    using class_name = HttpRequester*;
    static class_name capture(HttpRequester *value) { return value; }
};
}
extern "C" int LiWaitForConnectionCallbacks(void);
