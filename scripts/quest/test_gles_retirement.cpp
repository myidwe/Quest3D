// Executes the production resource owner's complete retire() body against real
// Mesa EGL/GLES. Only Android Java/ANativeWindow and selected failures are stubs.
#include "video/gles_surface_retirement.h"
#include "video/frame_identity.h"
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <thread>
#include <type_traits>

static_assert(!std::is_default_constructible_v<godot::GlesSurfaceRetirement>);
static_assert(!std::is_default_constructible_v<godot::GlesSurfaceRetirementWatch>);
static_assert(!std::is_default_constructible_v<godot::GlesSurfaceRetirementOwner>);

int inject_wait = 0;
int inject_delete = 0;
extern "C" GLenum glClientWaitSync(GLsync fence, GLbitfield flags, GLuint64 timeout) {
    if (inject_wait == 1) return GL_TIMEOUT_EXPIRED;
    if (inject_wait == 2) return GL_WAIT_FAILED;
    using Fn = GLenum (*)(GLsync, GLbitfield, GLuint64);
    static auto real = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, "glClientWaitSync"));
    assert(real);
    return real(fence, flags, timeout);
}
extern "C" void glDeleteTextures(GLsizei count, const GLuint *names) {
    if (inject_delete) { glBindTexture(0xffffffff, 0); return; }
    using Fn = void (*)(GLsizei, const GLuint *);
    static auto real = reinterpret_cast<Fn>(dlsym(RTLD_NEXT, "glDeleteTextures"));
    assert(real);
    real(count, names);
}

struct JavaFixture {
    JNINativeInterface table{};
    JNIEnv env{};
    int release_calls = 0, deletes = 0;
    bool exception = false, fail_release = false;
    static JavaFixture *current;
    JavaFixture() {
        current = this;
        table.CallVoidMethodV = [](JNIEnv *, jobject, jmethodID, va_list) {
            ++current->release_calls;
            current->exception = current->fail_release;
        };
        table.ExceptionCheck = [](JNIEnv *) -> jboolean { return current->exception; };
        table.ExceptionClear = [](JNIEnv *) { current->exception = false; };
        table.DeleteGlobalRef = [](JNIEnv *, jobject) { ++current->deletes; };
        env.functions = &table;
    }
};
JavaFixture *JavaFixture::current = nullptr;
extern "C" void ANativeWindow_release(ANativeWindow *window) { ++window->releases; }

namespace godot {
// Friend fixture seeds actual GL resources, then invokes the same production
// owner body TextureUploader invokes. It does not replace the deletion code.
class TextureUploader {
public:
    GlesSurfaceRetirementOwner owner;
    quest3d::OesLeaseState lease;
    ANativeWindow window;
    bool begin(std::string transport = std::string(32, 'a')) { return owner.begin(transport); }
    auto watch() const { return std::shared_ptr<const GlesSurfaceRetirementWatch>(owner.watch_); }
    bool request(GlesSurfaceLifetime lifetime, bool strict = true) { return owner.request(lifetime, strict); }
    bool can_create(uint64_t queued_generation) const {
        return owner.accepts_create(queued_generation);
    }
    bool accepts_failure(GlesSurfaceLifetime scope, const std::string &transport) const {
        return owner.accepts_failure(scope, transport);
    }
    void create_resources(bool partial = false) {
        owner.context_ = eglGetCurrentContext();
        owner.window = &window;
        owner.surface = reinterpret_cast<void *>(1);
        owner.matrix = reinterpret_cast<void *>(2);
        owner.release_method = reinterpret_cast<void *>(3);
        auto texture = [](unsigned &name) {
            glGenTextures(1, &name); glBindTexture(GL_TEXTURE_2D, name);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 4, 4, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
        };
        texture(owner.oes_texture); texture(owner.output_texture); texture(owner.depth_texture);
        glGenFramebuffers(1, &owner.fbo); glBindFramebuffer(GL_FRAMEBUFFER, owner.fbo);
        glGenFramebuffers(1, &owner.depth_fbo); glBindFramebuffer(GL_FRAMEBUFFER, owner.depth_fbo);
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        // A sparse PBO array reproduces partial initialization (index 0 absent).
        for (int i = partial ? 1 : 0; i < 3; ++i) {
            glGenBuffers(1, &owner.pbos[i]); glBindBuffer(GL_PIXEL_PACK_BUFFER, owner.pbos[i]);
            glBufferData(GL_PIXEL_PACK_BUFFER, 64, nullptr, GL_STREAM_READ);
            owner.depth_fences[i] = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        }
        glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        owner.program = glCreateProgram();
        owner.ready_fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        owner.consumer_fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        glFlush();
        assert(glGetError() == GL_NO_ERROR);
        owner.initialized_ = !partial;
    }
    void legacy() { owner.legacy_output_used_ = true; }
    unsigned texture() const { return owner.oes_texture; }
    bool resources_present() const { return owner.has_gl_resources() || owner.surface || owner.matrix || owner.window; }
    void retire(JNIEnv *env) { owner.retire(lease.held(), env); }
    void mark_queued_server_missing() { owner.publish(GlesRetirementStatus::RenderServerUnavailable); }
    // Cleanup only fixture allocations after terminal failure; this deliberately
    // never changes the production watch or models a production recovery API.
    void free_fixture_allocations() {
        if (owner.consumer_fence) glDeleteSync(reinterpret_cast<GLsync>(owner.consumer_fence));
        if (owner.ready_fence) glDeleteSync(reinterpret_cast<GLsync>(owner.ready_fence));
        if (owner.retirement_fence_) glDeleteSync(owner.retirement_fence_);
        for (auto fence : owner.depth_fences) if (fence) glDeleteSync(reinterpret_cast<GLsync>(fence));
        glDeleteBuffers(3, owner.pbos);
        glDeleteFramebuffers(1, &owner.fbo); glDeleteFramebuffers(1, &owner.depth_fbo);
        glDeleteTextures(1, &owner.oes_texture); glDeleteTextures(1, &owner.output_texture);
        glDeleteTextures(1, &owner.depth_texture); glDeleteProgram(owner.program);
        assert(glGetError() == GL_NO_ERROR);
    }
};
}

int main() {
    using namespace godot;
    using GetDisplay = EGLDisplay (*)(EGLenum, void *, const EGLint *);
    auto get_display = reinterpret_cast<GetDisplay>(eglGetProcAddress("eglGetPlatformDisplayEXT"));
    assert(get_display);
    EGLDisplay display = get_display(0x31DD, nullptr, nullptr); // surfaceless Mesa
    EGLint major, minor;
    assert(eglInitialize(display, &major, &minor));
    assert(eglBindAPI(EGL_OPENGL_ES_API));
    const EGLint attrs[] = {EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_NONE};
    EGLConfig config; EGLint count;
    assert(eglChooseConfig(display, attrs, &config, 1, &count) && count == 1);
    const EGLint size[] = {EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE};
    auto surface = eglCreatePbufferSurface(display, config, size);
    const EGLint version[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    auto context = eglCreateContext(display, config, EGL_NO_CONTEXT, version);
    assert(surface != EGL_NO_SURFACE && context != EGL_NO_CONTEXT);
    assert(eglMakeCurrent(display, surface, surface, context));
    assert(std::strstr(reinterpret_cast<const char *>(glGetString(GL_RENDERER)), "llvmpipe"));
    std::printf("EGL %d.%d; actual GLES renderer: %s\n", major, minor, glGetString(GL_RENDERER));
    int cases = 0;
    auto complete = [](TextureUploader &uploader, JNIEnv *env) {
        for (int i = 0; i < 1000 && !uploader.watch()->snapshot()->resources_released(); ++i) {
            uploader.retire(env);
            std::this_thread::yield();
        }
        assert(uploader.watch()->snapshot()->resources_released());
    };
    std::shared_ptr<const GlesSurfaceRetirementWatch> retained;
    {
        JavaFixture java;
        TextureUploader uploader;
        assert(uploader.begin()); retained = uploader.watch();
        const auto first = retained->lifetime();
        assert(uploader.accepts_failure(first, first.transport));
        assert(!uploader.accepts_failure(first, std::string(32, 'b')));
        assert(!retained->snapshot()); // allocation/request is never completion
        uploader.create_resources();
        assert(!uploader.begin());
        auto wrong = first; ++wrong.instance;
        assert(!uploader.request(wrong));
        assert(!uploader.accepts_failure(wrong, first.transport));
        assert(uploader.request(first));
        assert(!uploader.accepts_failure(first, first.transport)); // queued old failure during retirement
        assert(uploader.lease.publish());
        auto serial = uploader.lease.acquire(); assert(serial);
        uploader.retire(&java.env);
        assert(retained->snapshot()->status() == GlesRetirementStatus::LeaseHeld);
        assert(uploader.resources_present() && java.release_calls == 0);
        assert(!uploader.begin());
        assert(uploader.lease.release(serial));
        inject_wait = 1; uploader.retire(&java.env);
        assert(retained->snapshot()->status() == GlesRetirementStatus::FencePending);
        assert(java.release_calls == 0 && uploader.resources_present());
        inject_wait = 0;
        complete(uploader, &java.env);
        auto final = retained->snapshot();
        assert(final->initialized_surface_retired() && java.release_calls == 1 && java.deletes == 2 && uploader.window.releases == 1);
        assert(!uploader.resources_present());
        uploader.retire(&java.env);
        assert(retained->snapshot() == final); // duplicate does not release twice
        assert(uploader.begin(std::string(32, 'b')));
        auto second = uploader.watch();
        assert(second->lifetime().generation == first.generation + 1);
        assert(second->lifetime().transport != first.transport);
        assert(!uploader.accepts_failure(first, second->lifetime().transport)); // deferred old failure after fresh start
        assert(!uploader.request(first) && !second->snapshot());
        assert(!uploader.can_create(first.generation)); // delayed old create/update
        assert(uploader.can_create(second->lifetime().generation));
        assert(uploader.request(second->lifetime()));
        assert(!uploader.can_create(second->lifetime().generation)); // cancelled create
        complete(uploader, nullptr);
        assert(!second->snapshot()->initialized());
        cases += 12;
    }
    assert(retained->snapshot()->initialized_surface_retired()); // survives destruction
    ++cases;
    {
        JavaFixture java; TextureUploader uploader; assert(uploader.begin()); uploader.create_resources(true);
        uploader.request(uploader.watch()->lifetime());
        uploader.retire(nullptr); glFinish(); uploader.retire(nullptr);
        assert(uploader.watch()->snapshot()->status() == GlesRetirementStatus::JniUnavailable);
        assert(uploader.resources_present());
        complete(uploader, &java.env);
        assert(!uploader.watch()->snapshot()->initialized_surface_retired());
        cases += 2;
    }
    for (int failure = 0; failure < 4; ++failure) {
        JavaFixture java; TextureUploader uploader; assert(uploader.begin()); uploader.create_resources();
        uploader.request(uploader.watch()->lifetime());
        if (failure == 0) inject_wait = 2;
        if (failure == 1) java.fail_release = true;
        if (failure == 2) inject_delete = 1;
        if (failure == 3) assert(eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT));
        glFinish(); uploader.retire(&java.env);
        auto result = uploader.watch()->snapshot();
        assert(result->terminal_failure() && !result->resources_released());
        if (failure == 0) assert(result->status() == GlesRetirementStatus::FenceFailed);
        if (failure == 1) assert(result->status() == GlesRetirementStatus::JniReleaseFailed && java.deletes == 0);
        if (failure == 2) assert(result->status() == GlesRetirementStatus::GlFailed && result->gl_error() != GL_NO_ERROR);
        if (failure == 3) assert(result->status() == GlesRetirementStatus::WrongContext);
        inject_wait = inject_delete = 0; java.fail_release = false;
        assert(eglMakeCurrent(display, surface, surface, context));
        uploader.request(uploader.watch()->lifetime()); uploader.retire(&java.env); uploader.mark_queued_server_missing();
        assert(uploader.watch()->snapshot() == result && !uploader.begin());
        uploader.free_fixture_allocations();
        ++cases;
    }
    {
        JavaFixture java; TextureUploader uploader; assert(uploader.begin()); uploader.create_resources(); uploader.legacy();
        uploader.request(uploader.watch()->lifetime()); uploader.retire(&java.env);
        assert(uploader.watch()->snapshot()->status() == GlesRetirementStatus::LegacyConsumerUnconfirmed);
        assert(uploader.resources_present() && !uploader.begin() && java.release_calls == 0);
        uploader.free_fixture_allocations(); ++cases;
    }
    {
        // Existing reconnect remains usable without manufacturing a strict
        // legacy retirement proof from its ordinary GL/JNI cleanup.
        JavaFixture java; TextureUploader uploader; assert(uploader.begin()); uploader.create_resources(); uploader.legacy();
        auto old = uploader.watch();
        uploader.request(old->lifetime(), false); uploader.retire(&java.env);
        assert(old->snapshot()->status() == GlesRetirementStatus::LegacyConsumerUnconfirmed);
        assert(old->snapshot()->storage_cleanup_returned() && !old->snapshot()->resources_released());
        assert(!uploader.resources_present() && uploader.begin(std::string(32, 'b')));
        assert(!uploader.request(old->lifetime()));
        uploader.request(uploader.watch()->lifetime()); complete(uploader, nullptr);
        ++cases;
    }
    {
        // A strict request wins over a queued ordinary request and remains
        // strict after repeated ordinary cleanup/reconnect attempts.
        JavaFixture java; TextureUploader uploader; assert(uploader.begin()); uploader.create_resources(); uploader.legacy();
        auto old = uploader.watch();
        uploader.request(old->lifetime(), false); uploader.request(old->lifetime());
        uploader.request(old->lifetime(), false); uploader.retire(&java.env);
        assert(!old->snapshot()->storage_cleanup_returned() && uploader.resources_present() && !uploader.begin());
        uploader.free_fixture_allocations(); ++cases;
    }
    {
        JavaFixture java; TextureUploader uploader; assert(uploader.begin()); uploader.create_resources(); uploader.legacy();
        auto old = uploader.watch(); uploader.request(old->lifetime(), false); uploader.retire(&java.env);
        uploader.request(old->lifetime()); uploader.retire(&java.env);
        assert(old->snapshot()->storage_cleanup_returned() && !old->snapshot()->resources_released());
        assert(!uploader.begin()); ++cases;
    }
    {
        TextureUploader uploader; assert(uploader.begin()); retained = uploader.watch();
        uploader.request(retained->lifetime()); uploader.mark_queued_server_missing();
        assert(retained->snapshot()->status() == GlesRetirementStatus::RenderServerUnavailable);
    }
    assert(retained->snapshot()->status() == GlesRetirementStatus::OwnerDestroyed); ++cases;
    assert(eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT));
    assert(eglDestroyContext(display, context)); assert(eglDestroySurface(display, surface)); assert(eglTerminate(display));
    std::printf("PASS: %d production-owner checks; GL real; JNI/window and injected errors are fixtures; no Android speaker/headset claim\n", cases);
}
