"""Execute production picture-grade functions in private Mesa GLES.

This tests actual shader arithmetic and neutral compatibility. It does not test
Android OES decode, OpenXR composition, or the physical headset white point.
Run with the WSL standard-library Python used by the pinned native build tools.
"""
import argparse
import ctypes as c
import hashlib
import json
import math
import os
from pathlib import Path
import re


def extract_function(source, name):
    start = source.index('vec3 ' + name + '(')
    body = source.index('{', start)
    level = 1
    end = body + 1
    while level:
        level += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.environ['LIBGL_ALWAYS_SOFTWARE'] = 'true'
    os.environ['GALLIUM_DRIVER'] = 'llvmpipe'
    egl = c.CDLL('libEGL.so.1')
    gl = None  # Resolve GLES through EGL; no extra system package required.
    def bind(lib, name, result, types):
        if lib is None:
            address = getproc(name.encode())
            assert address, name
            return c.CFUNCTYPE(result, *types)(address)
        f = getattr(lib, name); f.restype = result; f.argtypes = types
        return f
    void = c.c_void_p; integer = c.c_int; uint = c.c_uint; pointer = c.POINTER
    getproc = bind(egl, 'eglGetProcAddress', void, [c.c_char_p])
    getdisplay = c.CFUNCTYPE(void, uint, void, pointer(integer))(getproc(b'eglGetPlatformDisplayEXT'))
    display = getdisplay(0x31DD, None, None)
    initialize = bind(egl, 'eglInitialize', uint, [void,pointer(integer),pointer(integer)])
    major, minor = integer(), integer()
    assert initialize(display,c.byref(major),c.byref(minor))
    choose = bind(egl,'eglChooseConfig',uint,[void,pointer(integer),pointer(void),integer,pointer(integer)])
    attrs=(integer*13)(0x3033,1,0x3040,0x40,0x3024,8,0x3023,8,0x3022,8,0x3021,8,0x3038)
    config=void(); count=integer()
    assert choose(display,attrs,c.byref(config),1,c.byref(count)) and count.value==1
    assert bind(egl,'eglBindAPI',uint,[uint])(0x30A0)
    surface=bind(egl,'eglCreatePbufferSurface',void,[void,void,pointer(integer)])(display,config,(integer*5)(0x3057,1,0x3056,1,0x3038))
    context=bind(egl,'eglCreateContext',void,[void,void,void,pointer(integer)])(display,config,None,(integer*3)(0x3098,3,0x3038))
    assert surface and context
    current=bind(egl,'eglMakeCurrent',uint,[void,void,void,void])
    assert current(display,surface,surface,context)
    renderer=bind(gl,'glGetString',c.c_char_p,[uint])(0x1F01).decode()
    assert 'llvmpipe' in renderer
    create_shader=bind(gl,'glCreateShader',uint,[uint])
    shader_source=bind(gl,'glShaderSource',None,[uint,integer,pointer(c.c_char_p),pointer(integer)])
    compile_shader=bind(gl,'glCompileShader',None,[uint])
    shader_iv=bind(gl,'glGetShaderiv',None,[uint,uint,pointer(integer)])
    shader_log=bind(gl,'glGetShaderInfoLog',None,[uint,integer,pointer(integer),c.c_char_p])
    create_program=bind(gl,'glCreateProgram',uint,[])
    attach=bind(gl,'glAttachShader',None,[uint,uint]); link=bind(gl,'glLinkProgram',None,[uint])
    program_iv=bind(gl,'glGetProgramiv',None,[uint,uint,pointer(integer)])
    use=bind(gl,'glUseProgram',None,[uint])
    loc=bind(gl,'glGetUniformLocation',integer,[uint,c.c_char_p])
    u1=bind(gl,'glUniform1f',None,[integer,c.c_float])
    u3=bind(gl,'glUniform3f',None,[integer,c.c_float,c.c_float,c.c_float])
    draw=bind(gl,'glDrawArrays',None,[uint,integer,integer])
    read=bind(gl,'glReadPixels',None,[integer,integer,integer,integer,uint,uint,void])
    error=bind(gl,'glGetError',uint,[])
    bind(gl,'glViewport',None,[integer,integer,integer,integer])(0,0,1,1)
    bind(gl,'glDisable',None,[uint])(0x0BD0)  # No dithering in numeric fixture.
    def shader(kind,source):
        handle=create_shader(kind); data=c.c_char_p(source.encode()); shader_source(handle,1,c.byref(data),None); compile_shader(handle)
        ok=integer(); shader_iv(handle,0x8B81,c.byref(ok))
        if not ok.value:
            log=c.create_string_buffer(8192); shader_log(handle,len(log),None,log); raise AssertionError(log.value)
        return handle
    vertex=shader(0x8B31,'#version 300 es\nvoid main(){vec2 p=vec2((gl_VertexID<<1)&2,gl_VertexID&2);gl_Position=vec4(p*2.0-1.0,0,1);}')
    root=args.source
    helper=(root/'src/shaders/picture_color.gdshaderinc').read_text()
    native_path=root/'extensions/nightfall-xr/src/fast_xr_renderer_android.cpp'
    native=native_path.read_text()
    functions={}
    for label,symbol in [('native_sdr','FRAGMENT_SRC'),('native_hdr','HDR_FRAGMENT_SRC')]:
        block=native.split('static const char *'+symbol+' =',1)[1].split('\n\n',1)[0]
        decoded=''.join(json.loads(v) for v in re.findall(r'"(?:[^"\\]|\\.)*"',block))
        extracted='\n'.join(extract_function(decoded,n) for n in ('picture_color_to_linear','picture_color_to_srgb','picture_color_encoded'))
        expected='\n'.join(extract_function(helper,n) for n in ('picture_color_to_linear','picture_color_to_srgb','picture_color_encoded'))
        assert extracted==expected, 'Native and fallback transfer functions diverged'
        functions[label]=(extracted+'\n'+extract_function(decoded,'applyPicture'),'applyPicture','u_')
    for label,path in [('canvas_sdr','yuv_display_core.gdshaderinc'),('canvas_hdr','yuv_display_hdr.gdshader'),('mesh','stereo_screen_core.gdshaderinc')]:
        body=extract_function((root/'src/shaders'/path).read_text(),'apply_picture')
        functions[label]=(helper+'\n'+body,'apply_picture','')
    colors=[(0,0,0),(.04,.04,.04),(.18,.18,.18),(.5,.5,.5),(1,1,1),(.9,.2,.1),(.1,.9,.4),(.2,.3,.95)]
    gains=[(1,1,1),(2/3,5/6,1),(1,5/6,2/3),(.75,1,.75),(1,8/11,1)]
    grades=[(0,1,1),(.1,1,1),(-.1,1.25,.75)]
    def linear(v): return v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4
    def encoded(v): return v*12.92 if v<=.0031308 else 1.055*v**(1/2.4)-.055
    results=[]; comparison={}
    for label,(body,name,prefix) in functions.items():
        # Mesh has a linear output; encode fixture output exactly once for comparison.
        call=name+'(input_color)'
        if label=='mesh': call='picture_color_to_srgb('+call+')'
        fragment='#version 300 es\nprecision highp float;\nuniform vec3 input_color;\nuniform vec3 picture_color_gain;\n'
        fragment+='\n'.join('uniform float '+prefix+n+';' for n in ('brightness','contrast','gamma'))
        fragment+='\n'+body+'\nout vec4 result;\nvoid main(){result=vec4('+call+',1.0);}'
        fs=shader(0x8B30,fragment); program=create_program(); attach(program,vertex); attach(program,fs); link(program)
        ok=integer(); program_iv(program,0x8B82,c.byref(ok)); assert ok.value
        use(program); max_error=0; samples=0
        for brightness,contrast,gamma in grades:
            for name,value in [('brightness',brightness),('contrast',contrast),('gamma',gamma)]:
                u1(loc(program,(prefix+name).encode()),value)
            for gain in gains:
                u3(loc(program,b'picture_color_gain'),*gain)
                for color in colors:
                    source=tuple(linear(x) for x in color) if label=='mesh' else color
                    u3(loc(program,b'input_color'),*source); draw(4,0,3)
                    pixels=(c.c_ubyte*4)(); read(0,0,1,1,0x1908,0x1401,pixels); assert error()==0
                    graded=[max((x-.5)*contrast+.5+brightness,0)**(1/gamma) for x in source]
                    expected=[encoded(x*g) if label=='mesh' else (x if gain==(1,1,1) else encoded(linear(x)*g)) for x,g in zip(graded,gain)]
                    quantized=[round(max(0,min(1,x))*255) for x in expected]
                    delta=max(abs(pixels[i]-quantized[i]) for i in range(3)); assert delta<=1,(label,color,gain,tuple(pixels),quantized)
                    max_error=max(max_error,delta); samples+=1
                    if (brightness,contrast,gamma)==(0,1,1):
                        key=(gain,color)
                        previous=comparison.setdefault(key,tuple(pixels))
                        assert max(abs(previous[i]-pixels[i]) for i in range(3))<=1,(label,key,previous,tuple(pixels))
        results.append({'variant':label,'draws':samples,'max_rgb_error_8bit':max_error})
        bind(gl,'glDeleteProgram',None,[uint])(program); bind(gl,'glDeleteShader',None,[uint])(fs)
    bind(gl,'glDeleteShader',None,[uint])(vertex)
    assert current(display,None,None,None)
    assert bind(egl,'eglDestroyContext',uint,[void,void])(display,context)
    assert bind(egl,'eglDestroySurface',uint,[void,void])(display,surface)
    assert bind(egl,'eglTerminate',uint,[void])(display)
    report={'passed':True,'renderer':renderer,'cases':results,'native_source_sha256':hashlib.sha256(native_path.read_bytes()).hexdigest(),'physical_quest_verified':False,'limitations':'Production grading functions executed, not full OES/OpenXR pipeline. Existing B/C/G domain differs in mesh; color-only neutral-grade parity checked.'}
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(report,indent=2)+'\n'); print(json.dumps(report))


if __name__=='__main__': main()
