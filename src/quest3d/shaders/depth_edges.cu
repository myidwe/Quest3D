// Pinned NVRTC 12.6, compute_75, --fmad=false; reference: depth_edges.py.
// Header-free NVRTC device intrinsics. No atomics, sorting, or filled donors.

__device__ int qmin(int a, int b) { return a < b ? a : b; }
__device__ int qmax(int a, int b) { return a > b ? a : b; }
__device__ float bounded(float v, float lo, float hi) {
    return fminf(hi, fmaxf(lo, v));
}
__device__ float smooth_gate(float v, float lo, float span) {
    float t = bounded((v - lo) / span, 0.0f, 1.0f);
    return (t * t) * (3.0f - 2.0f * t);
}
__device__ float bilinear(const float *source, int width, int height,
                         float x, float y) {
    int x0 = qmin((int)floorf(x), width - 1);
    int y0 = qmin((int)floorf(y), height - 1);
    int x1 = qmin(x0 + 1, width - 1), y1 = qmin(y0 + 1, height - 1);
    float fx = x - floorf(x), fy = y - floorf(y);
    // Match Torch's y-then-x nested bilinear accumulation and endpoint policy.
    return (1.0f - fy) * ((1.0f - fx) * source[y0 * width + x0]
                         + fx * source[y0 * width + x1])
           + fy * ((1.0f - fx) * source[y1 * width + x0]
                   + fx * source[y1 * width + x1]);
}

extern "C" __global__ void quest3d_depth_low_prepare(
        const float *rgb, const float *depth, float *guide, float *curvature,
        int width, int height, int low_width, int low_height,
        float rgb_x_scale, float rgb_y_scale) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    int low_plane = low_width * low_height;
    if (i >= low_plane) return;
    int x = i % low_width, y = i / low_width, plane = width * height;
    for (int c = 0; c < 3; ++c)
        guide[c * low_plane + i] = bilinear(rgb + c * plane, width, height,
            (float)x * rgb_x_scale, (float)y * rgb_y_scale);
    float value = 0.0f;
    if (y > 0 && y + 1 < low_height)
        value = fabsf((depth[i - low_width] - 2.0f * depth[i]) + depth[i + low_width]);
    if (x > 0 && x + 1 < low_width)
        value = fmaxf(value, fabsf((depth[i - 1] - 2.0f * depth[i]) + depth[i + 1]));
    curvature[i] = value;
}

extern "C" __global__ void quest3d_depth_rgb_edge(
        const float *rgb, float *edge, int width, int height) {
    int i = blockIdx.x * blockDim.x + threadIdx.x, plane = width * height;
    if (i >= plane) return;
    int x = i % width, y = i / width;
    float result = 0.0f;
    for (int c = 0; c < 3; ++c) {
        const float *channel = rgb + c * plane;
        float value = channel[i];
        if (x > 0) result = fmaxf(result, fabsf(value - channel[i - 1]));
        if (x + 1 < width) result = fmaxf(result, fabsf(value - channel[i + 1]));
        if (y > 0) result = fmaxf(result, fabsf(value - channel[i - width]));
        if (y + 1 < height) result = fmaxf(result, fabsf(value - channel[i + width]));
    }
    edge[i] = result;
}

extern "C" __global__ void quest3d_depth_max_horizontal(
        const float *edge, float *output, int width, int height, int radius) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= width * height) return;
    int x = i % width, row = i - x;
    float value = 0.0f;
    for (int sx = qmax(0, x - radius); sx <= qmin(width - 1, x + radius); ++sx)
        value = fmaxf(value, edge[row + sx]);
    output[i] = value;
}

extern "C" __global__ void quest3d_depth_max_vertical(
        const float *edge, float *output, int width, int height, int radius) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= width * height) return;
    int x = i % width, y = i / width;
    float value = 0.0f;
    for (int sy = qmax(0, y - radius); sy <= qmin(height - 1, y + radius); ++sy)
        value = fmaxf(value, edge[sy * width + x]);
    output[i] = value;
}

extern "C" __global__ void quest3d_depth_bilinear(
        const float *depth, float *output, int width, int height,
        int low_width, int low_height, float depth_x_scale, float depth_y_scale) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i >= width * height) return;
    output[i] = bilinear(depth, low_width, low_height,
        (float)(i % width) * depth_x_scale, (float)(i / width) * depth_y_scale);
}

extern "C" __global__ void quest3d_depth_joint_blend(
        const float *rgb, const float *depth, const float *guide,
        const float *curvature, const float *nearby_edge, float *output,
        int width, int height, int low_width, int low_height,
        float depth_x_scale, float depth_y_scale, float strength, float max_correction) {
    int i = blockIdx.x * blockDim.x + threadIdx.x, plane = width * height;
    if (i >= plane) return;
    int low_plane = low_width * low_height;
    float x = (float)(i % width) * depth_x_scale, y = (float)(i / width) * depth_y_scale;
    int x0 = (int)floorf(x), y0 = (int)floorf(y);
    float fx = x - (float)x0, fy = y - (float)y0;
    float baseline = bilinear(depth, low_width, low_height, x, y);
    float numerator = 0.0f, denominator = 0.0f, curve = 0.0f;
    float dmin = 1.0f, dmax = 0.0f;
    float cmin[3] = {1.0f, 1.0f, 1.0f}, cmax[3] = {0.0f, 0.0f, 0.0f};
    for (int oy = 0; oy < 2; ++oy) {
        int iy = qmin(y0 + oy, low_height - 1);
        float wy = oy ? fy : (1.0f - fy);
        for (int ox = 0; ox < 2; ++ox) {
            int ix = qmin(x0 + ox, low_width - 1), sample = iy * low_width + ix;
            float wx = ox ? fx : (1.0f - fx), spatial = wy * wx;
            float distance2 = 0.0f;
            for (int c = 0; c < 3; ++c) {
                float colour = guide[c * low_plane + sample];
                float delta = rgb[c * plane + i] - colour;
                distance2 += delta * delta;
                if (spatial > 0.0f) {
                    cmin[c] = fminf(cmin[c], colour);
                    cmax[c] = fmaxf(cmax[c], colour);
                }
            }
            distance2 /= 3.0f;
            float weight = spatial * expf(-50.0f * distance2), donor = depth[sample];
            numerator += weight * donor;
            denominator += weight;
            curve += spatial * curvature[sample];
            if (spatial > 0.0f) {
                dmin = fminf(dmin, donor);
                dmax = fmaxf(dmax, donor);
            }
        }
    }
    float colour_range = fmaxf(cmax[0] - cmin[0],
        fmaxf(cmax[1] - cmin[1], cmax[2] - cmin[2]));
    float blend = strength * smooth_gate(curve, 0.03f, 0.09f);
    blend *= smooth_gate(dmax - dmin, 0.03f, 0.09f);
    blend *= smooth_gate(colour_range, 0.06f, 0.14f);
    blend *= smooth_gate(nearby_edge[i], 0.04f, 0.12f);
    if (!(blend > 0.0f) || !(denominator > 1.0e-12f)) {
        output[i] = bounded(baseline, 0.0f, 1.0f);
        return;
    }
    float proposal = bounded(numerator / denominator, dmin, dmax);
    float delta = blend * (proposal - baseline);
    float correction = max_correction * tanhf(delta / max_correction);
    output[i] = bounded(bounded(baseline + correction, dmin, dmax), 0.0f, 1.0f);
}
