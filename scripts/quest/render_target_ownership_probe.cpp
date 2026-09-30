// Diagnostic executable linked against the pinned Godot engine objects.
// All OpenGL work uses a private Mesa llvmpipe EGL pbuffer; no XR/device/network.
#include "platform/linuxbsd/os_linuxbsd.h"
#include "main/main.h"
#include "drivers/gles3/rasterizer_util_gles3.h"
#include "drivers/gles3/storage/config.h"
#include "drivers/gles3/storage/texture_storage.h"
#include "drivers/gles3/storage/utilities.h"
#include <dlfcn.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define REQUIRE(test) do { if (!(test)) { std::fprintf(stderr, "probe_failed line=%d: %s\n", __LINE__, #test); std::exit(2); } } while (false)

void __cpuid(int *cpu, int info) {
	__asm__ __volatile__("xchgq %%rbx, %q1; cpuid; xchgq %%rbx, %q1;"
		: "=a"(cpu[0]), "=r"(cpu[1]), "=c"(cpu[2]), "=d"(cpu[3]) : "0"(info));
}

int main(int argc, char **argv) {
	REQUIRE(argc == 2);
	setenv("LIBGL_ALWAYS_SOFTWARE", "true", 1);
	setenv("GALLIUM_DRIVER", "llvmpipe", 1);
	OS_LinuxBSD os;
	char headless[] = "--headless";
	char path[] = "--path";
	char *args[] = { headless, path, argv[1] };
	REQUIRE(Main::setup(argv[0], 3, args) == OK);
	auto lib = dlopen("libEGL.so.1", RTLD_NOW | RTLD_LOCAL);
	REQUIRE(lib);
	auto proc = reinterpret_cast<void *(*)(const char *)>(dlsym(lib, "eglGetProcAddress"));
	REQUIRE(proc);
	auto get_display = reinterpret_cast<void *(*)(unsigned, void *, const int *)>(proc("eglGetPlatformDisplayEXT"));
	auto initialize = reinterpret_cast<unsigned (*)(void *, int *, int *)>(dlsym(lib, "eglInitialize"));
	auto choose = reinterpret_cast<unsigned (*)(void *, const int *, void **, int, int *)>(dlsym(lib, "eglChooseConfig"));
	auto bind = reinterpret_cast<unsigned (*)(unsigned)>(dlsym(lib, "eglBindAPI"));
	auto create_surface = reinterpret_cast<void *(*)(void *, void *, const int *)>(dlsym(lib, "eglCreatePbufferSurface"));
	auto create_context = reinterpret_cast<void *(*)(void *, void *, void *, const int *)>(dlsym(lib, "eglCreateContext"));
	auto make_current = reinterpret_cast<unsigned (*)(void *, void *, void *, void *)>(dlsym(lib, "eglMakeCurrent"));
	auto destroy_context = reinterpret_cast<unsigned (*)(void *, void *)>(dlsym(lib, "eglDestroyContext"));
	auto destroy_surface = reinterpret_cast<unsigned (*)(void *, void *)>(dlsym(lib, "eglDestroySurface"));
	auto terminate = reinterpret_cast<unsigned (*)(void *)>(dlsym(lib, "eglTerminate"));
	REQUIRE(get_display && initialize && choose && bind && create_surface && create_context && make_current && destroy_context && destroy_surface && terminate);
	auto display = get_display(0x31dd, nullptr, nullptr); // EGL_PLATFORM_SURFACELESS_MESA.
	int major = 0, minor = 0;
	REQUIRE(initialize(display, &major, &minor));
	int config_attrs[] = {0x3033, 1, 0x3040, 0x40, 0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3038};
	void *egl_config = nullptr;
	int count = 0;
	REQUIRE(choose(display, config_attrs, &egl_config, 1, &count) && count == 1);
	REQUIRE(bind(0x30a0)); // OpenGL ES.
	int surface_attrs[] = {0x3057, 64, 0x3056, 64, 0x3038};
	int context_attrs[] = {0x3098, 3, 0x3038};
	auto surface = create_surface(display, egl_config, surface_attrs);
	auto context = create_context(display, egl_config, nullptr, context_attrs);
	REQUIRE(surface && context && make_current(display, surface, surface, context));
	REQUIRE(gladLoadGLES2(reinterpret_cast<GLADloadfunc>(proc)));
	const auto renderer = reinterpret_cast<const char *>(glGetString(GL_RENDERER));
	REQUIRE(renderer && std::strstr(renderer, "llvmpipe"));
	std::printf("renderer=%s egl=%d.%d\n", renderer, major, minor);
	std::fflush(stdout);
	RasterizerUtilGLES3::set_gles_over_gl(false);
	{
		GLES3::Config config;
		GLES3::Utilities utilities;
		GLES3::TextureStorage storage;
		for (int iteration = 0; iteration < 40; ++iteration) {
			RID rt = storage.render_target_create();
			storage.render_target_set_size(rt, 64, 64, 1);
			RID owned = storage.get_render_target(rt)->texture;
			RID first = storage.texture_allocate();
			RID second = storage.texture_allocate();
			Ref<Image> image = Image::create_empty(64, 64, false, Image::FORMAT_RGBA8);
			image->fill(Color(1, 0, 0, 1));
			storage.texture_2d_initialize(first, image);
			storage.texture_2d_initialize(second, image);
			storage.render_target_set_override(rt, first, RID(), RID(), RID());
			storage.render_target_set_override(rt, second, RID(), RID(), RID());
			storage.render_target_set_override(rt, first, RID(), RID(), RID()); // Real FBO cache hit.
			bool owned_preserved = storage.get_render_target(rt)->texture == owned;
			std::vector<GLuint> cached_depth_names;
			for (const auto &entry : storage.get_render_target(rt)->overridden.fbo_cache) {
				for (GLuint texture : entry.value.allocated_textures) cached_depth_names.push_back(texture);
			}
			std::printf("iteration=%d cache_hit_owned_preserved=%d\n", iteration, owned_preserved);
			std::fflush(stdout);
			storage.render_target_set_override(rt, RID(), RID(), RID(), RID());
			for (GLuint texture : cached_depth_names) REQUIRE(!glIsTexture(texture));
			std::printf("iteration=%d cached_gl_names_deleted=%zu\n", iteration, cached_depth_names.size());
			// The compositor owns these external images and may retire them now.
			// Each was marked by the production override path; retire the unused second image separately.
			storage.get_texture(second)->is_render_target = false;
			storage.get_texture(second)->render_target = nullptr;
			storage.texture_free(second);
			storage.texture_free(first);
			std::printf("iteration=%d external_retired=%d old_owned_live=%d before_resize\n", iteration,
				storage.get_texture(first) == nullptr, storage.get_texture(owned) != nullptr);
			std::fflush(stdout);
			storage.render_target_set_size(rt, 80 + iteration, 48, 1); // Same call path as the tombstone.
			REQUIRE(owned_preserved);
			REQUIRE(storage.get_render_target(rt)->texture == owned && storage.get_texture(owned));
			storage.render_target_free(rt);
			REQUIRE(storage.get_texture(owned) == nullptr);
			REQUIRE(glGetError() == GL_NO_ERROR);
		}
		std::puts("PASS: 40 production GLES cache-hit -> detach -> external-free -> resize -> RT-free cycles");
	}
	REQUIRE(make_current(display, nullptr, nullptr, nullptr));
	REQUIRE(destroy_context(display, context));
	REQUIRE(destroy_surface(display, surface));
	REQUIRE(terminate(display));
	dlclose(lib);
	Main::cleanup();
	return 0;
}
