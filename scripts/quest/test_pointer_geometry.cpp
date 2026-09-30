#include "pointer_geometry.h"
#include <cassert>
#include <iostream>
using namespace quest3d;
static void near(double a, double b) { assert(std::abs(a-b) < 1e-10); }
static PointerPanel quad(bool bezel = true) {
    PointerPanel p; p.width = 3.24; p.height = 1.84;
    p.image_width = 1296; p.image_height = 736;
    p.viewport_x = p.viewport_y = bezel ? 8 : 0;
    p.viewport_width = p.image_width - 2*p.viewport_x;
    p.viewport_height = p.image_height - 2*p.viewport_y; return p;
}
int main() {
    auto p = quad();
    auto center = pointer_hit(p, {0,0,2}, {0,0,-37}, 2);
    assert(center.valid); near(center.u,.5); near(center.v,.5); near(center.distance,2);
    assert(!pointer_hit(p,{0,0,2},{0,0,-1},1.999).valid);
    assert(!pointer_hit(p,{0,0,-2},{0,0,1},10).valid); // exterior quad
    assert(!pointer_hit(p,{0,0,0},{0,0,-1},10).valid);
    assert(!pointer_hit(p,{0,0,2},{1,0,0},10).valid);
    assert(!pointer_hit(p,{0,0,2},{0,0,0},10).valid);
    assert(!pointer_hit(p,{2,0,2},{0,0,-1},10).valid);
    assert(!pointer_hit(p,{p.width/2,0,2},{0,0,-1},10).valid); // bezel
    // Golden emulates the ACTUAL vertex interpolation into glViewport and
    // packed SBS fragment coordinates. It covers both eyes and top/bottom flip,
    // including a non-centred odd viewport to expose assumed symmetric insets.
    for (bool bezel : {false,true}) for (int eye : {0,1}) {
        p = quad(bezel); p.encoded_eye = eye;
        for (int variant : {0,1}) {
            if (variant) { p.viewport_x=7; p.viewport_y=11; p.viewport_width=1279; p.viewport_height=719; }
            for (int i=1;i<20;++i) for (int j=1;j<20;++j) {
                const double gl_u=i/20.0, gl_v=j/20.0;
                const double px=p.viewport_x+gl_u*p.viewport_width, py=p.viewport_y+gl_v*p.viewport_height;
                const double x=(px/p.image_width-.5)*p.width, y=(py/p.image_height-.5)*p.height;
                auto hit=pointer_hit(p,{x,y,2},{0,0,-4},10);
                assert(hit.valid); near(hit.u,gl_u); near(hit.v,1-gl_v);
                near(hit.packed_u,gl_u*.5+eye*.5); near(hit.packed_v,gl_v);
            }
        }
    }
    // Closed geometrical boundary is represented as exactly 0/1, not clamped;
    // PC source bounds/pixel conversion are a later host decision.
    p=quad(false);
    auto corner=pointer_hit(p,{-p.width/2,p.height/2,1},{0,0,-1},10);
    assert(corner.valid); near(corner.u,0); near(corner.v,0);
    assert(!pointer_hit(p,{-p.width/2-1e-9,0,1},{0,0,-1},10).valid);
    auto cyl=quad(false); cyl.cylinder=true; cyl.radius=4; cyl.angle=1.2; cyl.aspect=2;
    auto hit=pointer_hit(cyl,{0,0,0},{0,0,-3},10);
    assert(hit.valid); near(hit.u,.5); near(hit.v,.5); near(hit.distance,4);
    // Official cylinder height is arcLength/aspectRatio (not multiplication).
    const double height=cyl.radius*cyl.angle/cyl.aspect;
    for (int i=1;i<20;++i) for (int j=1;j<20;++j) {
        const double u=i/20.0,v=j/20.0,theta=(u-.5)*cyl.angle;
        PointerVec point{cyl.radius*std::sin(theta),(.5-v)*height,-cyl.radius*std::cos(theta)};
        auto result=pointer_hit(cyl,{0,0,0},point,10);
        assert(result.valid); near(result.u,u); near(result.v,v);
        near(result.distance,std::hypot(point.x,point.y,point.z));
    }
    assert(!pointer_hit(cyl,{0,0,0},{0,height/2+1e-5,-4},10).valid);
    assert(!pointer_hit(cyl,{0,0,-6},{0,0,1},10).valid); // near exterior invisible, far side outside arc
    assert(!pointer_hit(cyl,{0,0,0},{0,0,1},10).valid); // rear angle outside arc
    assert(!pointer_hit(cyl,{5,0,0},{0,1,0},10).valid); // parallel/no cylinder hit
    assert(!pointer_hit(cyl,{4,0,-10},{0,0,1},30).valid); // tangent
    assert(!pointer_hit(cyl,{0,0,0},{3,0,-3},10).valid); // arc outside
    for (double bad : {NAN, INFINITY, -INFINITY}) {
        assert(!pointer_hit(p,{bad,0,2},{0,0,-1},10).valid);
        assert(!pointer_hit(p,{0,0,2},{0,bad,-1},10).valid);
        assert(!pointer_hit(p,{0,0,2},{0,0,-1},bad).valid);
        auto shape=cyl; shape.aspect=bad; assert(!pointer_hit(shape,{0,0,0},{0,0,-1},10).valid);
    }
    auto invalid=p; invalid.viewport_x=INT32_MAX; assert(!pointer_hit(invalid,{0,0,2},{0,0,-1},10).valid);
    invalid=p; invalid.viewport_width=0; assert(!pointer_hit(invalid,{0,0,2},{0,0,-1},10).valid);
    PointerGeneration state;
    assert(!state.current(1,1)); state.redraw=false; assert(state.current(1,1));
    state.change(false); assert(!state.current(1,1) && state.current(2,1));
    state.change(true); assert(!state.current(3,2)); state.redraw=false; assert(state.current(3,2));
    assert(!state.current(2,2) && !state.current(3,1));
    state.panel=INT64_MAX; state.change(false); assert(!state.current(0,2));
    state.change(false); assert(state.panel==0);
    float matrix[16]{1,0,0,0, 0,-1,0,0, 0,0,1,0, 0,1,0,1};
    auto sampled=pointer_sample(matrix,16,.25,.75);
    assert(sampled.valid); near(sampled.u,.25); near(sampled.v,.25);
    matrix[0]=.5; matrix[12]=.125; // storage crop is not logical eye letterbox
    sampled=pointer_sample(matrix,16,.25,.75);
    assert(sampled.valid); near(sampled.u,.25);
    for (int i=0;i<16;++i) {
        const float saved=matrix[i]; matrix[i]=NAN;
        assert(!pointer_sample(matrix,16,.25,.75).valid); matrix[i]=saved;
    }
    assert(!pointer_sample(matrix,15,.25,.75).valid);
    assert(!pointer_sample(nullptr,16,.25,.75).valid);
    matrix[12]=2; assert(!pointer_sample(matrix,16,.25,.75).valid);
    std::cout << "pointer geometry: shader goldens, quad/cylinder, bounds/finite, distance, generation PASS\n";
}
