#pragma once
struct ANativeWindow { int releases = 0; };
extern "C" void ANativeWindow_release(ANativeWindow *window);
