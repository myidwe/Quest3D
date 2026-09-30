#include <bits/stdc++.h>
#include <godot_cpp/godot.hpp>
#include <godot_cpp/classes/open_xr_extension_wrapper_extension.hpp>
#include <godot_cpp/classes/open_xr_interaction_profile_metadata.hpp>
#include <godot_cpp/classes/open_xrapi_extension.hpp>
#include <godot_cpp/classes/ref_counted.hpp>
#include "xr_retirement.h"
// Test-only access to arrange actual renderer resources without starting a
// headset session. The production TU is compiled unchanged, including Android.
#define private public
#include "fast_xr_renderer.h"
#undef private
#include <godot_cpp/godot.hpp>
#include <EGL/eglext.h>
#include <cassert>
using namespace godot;

namespace {
int destroyed = 0;
int finished = 0;
EGLContext expected_context = EGL_NO_CONTEXT;
bool fail_activate = false, fail_restore = false, fail_context = false, fail_surface = false, fail_gpu = false;
XrResult destroy_result = XR_SUCCESS;
NightfallXrRenderer *provider = nullptr;
int provider_unregistered = 0;
XrResult destroy_swapchain(XrSwapchain) {
    assert(eglGetCurrentContext()==expected_context && finished>0);
    ++destroyed;
    return destroy_result;
}
XrResult reject_acquire(XrSwapchain,const XrSwapchainImageAcquireInfo *,uint32_t *) { return XR_ERROR_RUNTIME_FAILURE; }
struct Mesa {
    EGLDisplay display;
    EGLSurface surface;
    EGLContext context;
    Mesa() {
        auto platform = reinterpret_cast<PFNEGLGETPLATFORMDISPLAYEXTPROC>(eglGetProcAddress("eglGetPlatformDisplayEXT"));
        assert(platform);
        display = platform(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, nullptr);
        EGLint major, minor;
        assert(eglInitialize(display, &major, &minor));
        assert(eglBindAPI(EGL_OPENGL_ES_API));
        const EGLint attrs[] = {EGL_SURFACE_TYPE,EGL_PBUFFER_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_ES3_BIT,EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_NONE};
        EGLConfig config; EGLint count;
        assert(eglChooseConfig(display, attrs, &config, 1, &count) && count == 1);
        const EGLint shape[] = {EGL_WIDTH,1,EGL_HEIGHT,1,EGL_NONE};
        surface=eglCreatePbufferSurface(display,config,shape);
        const EGLint version[] = {EGL_CONTEXT_CLIENT_VERSION,3,EGL_NONE};
        context=eglCreateContext(display,config,EGL_NO_CONTEXT,version);
        assert(surface && context && eglMakeCurrent(display,surface,surface,context));
        assert(std::strstr(reinterpret_cast<const char *>(glGetString(GL_RENDERER)),"llvmpipe"));
    }
    ~Mesa() {
        eglMakeCurrent(display,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);
        assert(eglDestroySurface(display,surface));
        assert(eglDestroyContext(display,context));
        assert(eglTerminate(display));
    }
};
}
extern "C" EGLBoolean __real_eglMakeCurrent(EGLDisplay,EGLSurface,EGLSurface,EGLContext);
extern "C" EGLBoolean __real_eglDestroyContext(EGLDisplay,EGLContext);
extern "C" EGLBoolean __real_eglDestroySurface(EGLDisplay,EGLSurface);
extern "C" void __real_glFinish();
// Only the XR provider registry is a stand-in. Godot ObjectDB, deferred calls,
// GDExtension dispatch and the production renderer are real.
extern "C" void __wrap__ZN5godot18OpenXRAPIExtension37unregister_composition_layer_providerEPNS_22OpenXRExtensionWrapperE(
        OpenXRAPIExtension *, OpenXRExtensionWrapper *object) {
    assert(object == provider && !provider->submission_cycle_open && provider->borrowed_layer_pointers.empty());
    assert(provider->owner_thread == std::this_thread::get_id());
    ++provider_unregistered;
    provider = nullptr;
}
extern "C" EGLBoolean __wrap_eglMakeCurrent(EGLDisplay d,EGLSurface draw,EGLSurface read,EGLContext c) {
    if ((fail_activate && c==expected_context) || (fail_restore && c!=expected_context)) return EGL_FALSE;
    return __real_eglMakeCurrent(d,draw,read,c);
}
extern "C" EGLBoolean __wrap_eglDestroyContext(EGLDisplay d,EGLContext c) {
    return fail_context ? EGL_FALSE : __real_eglDestroyContext(d,c);
}
extern "C" EGLBoolean __wrap_eglDestroySurface(EGLDisplay d,EGLSurface s) {
    return fail_surface ? EGL_FALSE : __real_eglDestroySurface(d,s);
}
extern "C" void __wrap_glFinish() {
    assert(eglGetCurrentContext()==expected_context);
    ++finished;
    __real_glFinish();
    if(fail_gpu)glEnable(0xffffffffU); // Inject a real GL error after the actual CPU wait.
}
class LeaseSourceProbe : public RefCounted {
    GDCLASS(LeaseSourceProbe, RefCounted)
protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("acquire_oes_frame"), &LeaseSourceProbe::acquire);
        ClassDB::bind_method(D_METHOD("release_oes_frame","serial","fence"), &LeaseSourceProbe::release);
    }
public:
    bool accept=true;
    int acquisitions=0, returns=0;
    GLsync consumer=nullptr;
    Dictionary acquire() {
        ++acquisitions;
        Dictionary d;
        d["lease_serial"]=77; d["texture_id"]=0; d["producer_fence"]=int64_t(0); d["color_transfer"]=0;
        PackedFloat32Array matrix; matrix.resize(16); matrix[0]=matrix[5]=matrix[10]=matrix[15]=1;
        d["matrix"]=matrix;
        return d;
    }
    bool release(int64_t serial,int64_t fence) {
        assert(serial==77); ++returns;
        if(accept)consumer=reinterpret_cast<GLsync>(uint64_t(fence));
        return accept;
    }
};
class XrRetirementProbe : public RefCounted {
    GDCLASS(XrRetirementProbe, RefCounted)
protected:
    static void _bind_methods() {
        ClassDB::bind_method(D_METHOD("run"), &XrRetirementProbe::run);
        ClassDB::bind_method(D_METHOD("begin_dispose", "borrowed"), &XrRetirementProbe::begin_dispose);
        ClassDB::bind_method(D_METHOD("observe_dispose"), &XrRetirementProbe::observe_dispose);
        ClassDB::bind_method(D_METHOD("end_dispose", "result", "cycle"), &XrRetirementProbe::end_dispose);
        ClassDB::bind_method(D_METHOD("dispose_off_owner"), &XrRetirementProbe::dispose_off_owner);
        ClassDB::bind_method(D_METHOD("finish_dispose", "failed"), &XrRetirementProbe::finish_dispose);
    }
public:
    std::unique_ptr<Mesa> lifetime_mesa;
    ObjectID lifetime_id;
    std::shared_ptr<const XrRetirementWatch> lifetime_watch;
    Object *begin_dispose(bool borrowed) {
        assert(!lifetime_mesa && !provider);
        lifetime_mesa=std::make_unique<Mesa>();
        auto *r=make(*lifetime_mesa);
        provider=r; provider_unregistered=0;
        lifetime_id=r->get_instance_id(); lifetime_watch=r->retirement_watch();
        if(borrowed)borrow(r);
        return r;
    }
    Dictionary observe_dispose() {
        Dictionary d;
        auto *object=ObjectDB::get_instance(lifetime_id);
        d["valid"]=object!=nullptr; d["provider_registered"]=provider!=nullptr;
        d["unregistered"]=provider_unregistered; d["destroyed"]=destroyed;
        auto retired=lifetime_watch->snapshot();
        d["retired"]=retired && retired->application_resources_retired();
        if(object)d["status"]=Object::cast_to<NightfallXrRenderer>(object)->get_shutdown_status();
        return d;
    }
    void end_dispose(int result,int cycle) {
        assert(provider && ObjectDB::get_instance(lifetime_id)==provider);
        provider->_quest3d_submission_boundary(notice(provider,"end",cycle,result));
    }
    void dispose_off_owner() {
        assert(provider);
        std::thread requester([&]{assert(!provider->request_dispose());}); requester.join();
    }
    void finish_dispose(bool failed) {
        if(failed) {
            assert(provider && ObjectDB::get_instance(lifetime_id)==provider && provider_unregistered==0);
            auto *r=provider; provider=nullptr;
            dispose(r,*lifetime_mesa); // Explicit fixture cleanup of a retained failure.
        } else {
            assert(!provider && !ObjectDB::get_instance(lifetime_id) && provider_unregistered==1);
        }
        lifetime_watch.reset(); lifetime_mesa.reset();
    }
    NightfallXrRenderer *make(Mesa &mesa, bool resources=true) {
        destroyed=finished=0;
        fail_activate=fail_restore=fail_context=fail_surface=fail_gpu=false;
        destroy_result=XR_SUCCESS;
        auto *r=memnew(NightfallXrRenderer);
        r->owner_thread=std::this_thread::get_id();
        r->registered_as_layer_provider=true;
        r->xr_session=reinterpret_cast<XrSession>(uint64_t(321));
        assert(r->begin_stream_lifetime());
        if(resources) {
            assert(r->init_egl());
            expected_context=r->egl_context;
            r->swapchain=reinterpret_cast<XrSwapchain>(uint64_t(123));
            r->overlay_swapchain=reinterpret_cast<XrSwapchain>(uint64_t(124));
            r->pfn_xrDestroySwapchain=&destroy_swapchain;
            glGenTextures(1,&r->upsample_texture);
            glBindTexture(GL_TEXTURE_2D,r->upsample_texture);
            glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,1,1,0,GL_RGBA,GL_UNSIGNED_BYTE,nullptr);
            glBindTexture(GL_TEXTURE_2D,0);
            assert(glGetError()==GL_NO_ERROR);
            r->stream_initialized=true;
            assert(eglMakeCurrent(mesa.display,mesa.surface,mesa.surface,mesa.context));
        }
        return r;
    }
    void dispose(NightfallXrRenderer *r, Mesa &mesa) {
        // Test-only disposal of deliberately retained failure resources. It
        // never changes or republishes the production immutable result.
        fail_activate=fail_restore=fail_context=fail_surface=fail_gpu=false;
        if(r->egl_context && r->egl_pbuffer) {
            assert(__real_eglMakeCurrent(mesa.display,r->egl_pbuffer,r->egl_pbuffer,r->egl_context));
            __real_glFinish();
            if(r->upsample_texture)glDeleteTextures(1,&r->upsample_texture);
        }
        assert(__real_eglMakeCurrent(mesa.display,mesa.surface,mesa.surface,mesa.context));
        if(r->egl_pbuffer)assert(__real_eglDestroySurface(mesa.display,r->egl_pbuffer));
        if(r->egl_context)assert(__real_eglDestroyContext(mesa.display,r->egl_context));
        r->registered_as_layer_provider=false;
        memdelete(r);
    }
    Dictionary notice(NightfallXrRenderer *r,const char *phase,int64_t cycle=44,int64_t result=0,bool layers=true) {
        Dictionary n;
        n["phase"]=phase; n["cycle"]=cycle; n["session"]=int64_t(uint64_t(r->xr_session));
        n["state_stamp"]=int64_t(XR_SESSION_STATE_FOCUSED); n["should_render"]=true; n["result"]=result;
        PackedInt64Array pointers;
        if(layers)pointers.push_back(int64_t(uint64_t(&r->quad_layers[0])));
        n["layers"]=pointers;
        return n;
    }
    void borrow(NightfallXrRenderer *r) {
        r->_quest3d_submission_boundary(notice(r,"begin"));
        r->cycle_prepared=true; r->cycle_layer_count=1;
        r->cycle_layer_pointers[0]=uint64_t(&r->quad_layers[0]);
        r->cycle_frame_descriptor["token_valid"]=true;
        r->cycle_frame_descriptor["transport_epoch"]=String(std::string(32,'a').c_str());
        assert(r->_get_composition_layer(0)!=0);
    }
    Dictionary run() {
        Mesa mesa;
        static_assert(!std::is_default_constructible<XrRetirement>::value);
        static_assert(!std::is_default_constructible<XrRetirementWatch>::value);
        int cases=0;
        auto *idle=memnew(NightfallXrRenderer);
        assert(!idle->stop_stream_observed(idle->lifetime()) && !idle->retirement_watch());
        memdelete(idle); ++cases;
        auto *renderer=make(mesa);
        auto watch=renderer->retirement_watch();
        const GLuint texture=renderer->upsample_texture;
        borrow(renderer);
        renderer->stop_stream(); renderer->stop_stream();
        const bool premature=destroyed!=0;
        assert(!premature && !watch->snapshot());
        renderer->_quest3d_submission_boundary(notice(renderer,"end",45));
        renderer->stop_stream();
        assert(destroyed==0 && !watch->snapshot());
        renderer->_quest3d_submission_boundary(notice(renderer,"state"));
        renderer->stop_stream();
        assert(destroyed==0 && !watch->snapshot());
        renderer->_quest3d_submission_boundary(notice(renderer,"end"));
        auto closed=watch->snapshot();
        assert(closed && closed->application_resources_retired() && closed->initialized());
        assert(closed->last_submitted_transport()==std::string(32,'a'));
        assert(closed->borrowed_layers_returned() && closed->gpu_commands_completed());
        assert(destroyed==2 && finished==1 && !glIsTexture(texture));
        const auto old=renderer->lifetime();
        assert(renderer->stop_stream_observed(old)==closed && destroyed==2);
        assert(renderer->begin_stream_lifetime());
        assert(!renderer->stop_stream_observed(old));
        assert(renderer->retirement_watch()!=watch && watch->snapshot()==closed);
        renderer->stop_stream();
        assert(renderer->retirement_watch()->snapshot()->application_resources_retired());
        dispose(renderer,mesa); cases+=5;
        for(int mode=0;mode<2;++mode) {
            auto *r=make(mesa); borrow(r); auto w=r->retirement_watch(); r->stop_stream();
            r->_quest3d_submission_boundary(notice(r,"end",44,mode==0?-1:0,mode==0));
            auto failed=w->snapshot();
            assert(failed && !failed->application_resources_retired());
            assert(failed->failure()==XrRetirementFailure::borrowed_end_unconfirmed && destroyed==0);
            r->_quest3d_submission_boundary(notice(r,"end")); r->stop_stream();
            assert(w->snapshot()==failed && destroyed==0 && !r->start(16,16,false));
            dispose(r,mesa); ++cases;
        }
        {
            auto *r=make(mesa); auto w=r->retirement_watch();
            std::thread requester([&]{r->stop_stream();}); requester.join();
            assert(!w->snapshot() && destroyed==0);
            r->stop_stream(); assert(w->snapshot()->application_resources_retired());
            dispose(r,mesa); ++cases;
        }
        for(int mode=0;mode<7;++mode) {
            auto *r=make(mesa); auto w=r->retirement_watch();
            if(mode==0)fail_activate=true;
            if(mode==1)fail_gpu=true;
            if(mode==2)destroy_result=XR_ERROR_RUNTIME_FAILURE;
            if(mode==3)destroy_result=XR_SESSION_LOSS_PENDING;
            if(mode==4)fail_restore=true;
            if(mode==5)fail_surface=true;
            if(mode==6)fail_context=true;
            r->stop_stream(); auto failed=w->snapshot();
            assert(failed && !failed->application_resources_retired() && !failed->owned_resources_absent());
            assert(!r->start(16,16,false));
            r->stop_stream(); assert(w->snapshot()==failed);
            if(mode<4)assert(r->swapchain && r->egl_context && r->egl_pbuffer);
            dispose(r,mesa); ++cases;
        }
        {
            auto *r=make(mesa,false); auto w=r->retirement_watch();
            r->stop_stream();
            assert(w->snapshot()->application_resources_retired() && !w->snapshot()->initialized());
            dispose(r,mesa); ++cases;
        }
        {
            auto *r=make(mesa); r->unreturned_oes_leases=1;
            auto w=r->retirement_watch(); r->stop_stream();
            assert(w->snapshot()->failure()==XrRetirementFailure::oes_lease_unreturned);
            assert(w->snapshot()->owned_resources_absent() && !w->snapshot()->consumer_leases_returned());
            dispose(r,mesa); ++cases;
        }
        for(bool accepted:{false,true}) {
            auto *r=make(mesa);
            Ref<LeaseSourceProbe> lease; lease.instantiate(); lease->accept=accepted;
            r->pending_frame_source=lease;
            r->pending_new_frame=true;
            r->pfn_xrAcquireSwapchainImage=&reject_acquire;
            // Exercise the real Android lease acquisition/consumer-fence/return
            // path. The XR acquire stub deliberately prevents a video draw.
            r->maybe_render_pending_frame();
            assert(lease->acquisitions==1 && lease->returns==1);
            assert(r->unreturned_oes_leases==(accepted?0:1));
            auto w=r->retirement_watch(); r->stop_stream();
            assert(w->snapshot()->application_resources_retired()==accepted);
            assert(r->pending_frame_source.is_null()==accepted);
            if(lease->consumer) {
                assert(glClientWaitSync(lease->consumer,0,0)==GL_ALREADY_SIGNALED);
                glDeleteSync(lease->consumer); lease->consumer=nullptr;
            }
            PackedFloat32Array matrix;
            assert(!r->submit_frame(false,0,0,0,matrix,1,1,false,0,false,false,0));
            dispose(r,mesa); ++cases;
        }
        Dictionary result;
        result["premature_destroy"]=premature;
        result["cases"]=cases;
        result["production_android_renderer_tu"]=true;
        result["gpu"]=String("Mesa llvmpipe GLES");
        result["openxr"]=String("explicit swapchain stub");
        result["quest_verified"]=false;
        return result;
    }
};
extern "C" GDExtensionBool GDE_EXPORT xr_retirement_probe_init(GDExtensionInterfaceGetProcAddress address,GDExtensionClassLibraryPtr library,GDExtensionInitialization *initialization) {
    GDExtensionBinding::InitObject init(address,library,initialization);
    init.register_initializer([](ModuleInitializationLevel level) {
        if(level!=MODULE_INITIALIZATION_LEVEL_SCENE)return;
        ClassDB::register_class<NightfallXrRenderer>();
        ClassDB::register_class<LeaseSourceProbe>();
        ClassDB::register_class<XrRetirementProbe>();
    });
    init.set_minimum_library_initialization_level(MODULE_INITIALIZATION_LEVEL_SCENE);
    return init.init();
}
