// Pinned NVRTC 12.6 / compute_75 / --fmad=false. Coefficients and replicated
// indices come unchanged from resample.py's pinned OpenCV four-tap reference.
// Explicit rounded operations preserve the materialized FP32 horizontal pass.
__device__ float horizontal(
        const unsigned char *source, long long row, long long x, int channel,
        long long source_width, long long stride_y, long long stride_x,
        long long stride_c, const long long *indices, const float *weights) {
    if (!indices) return (float)source[row * stride_y + x * stride_x + channel * stride_c];
    float result = 0.0f;
    for (int tap = 0; tap < 4; ++tap) {
        long long sx = indices[x * 4 + tap];
        // Internally generated indices are valid. Defensive bounds prevent an
        // accidental corrupted cache from turning into an out-of-range read.
        if (sx < 0 || sx >= source_width) return __int_as_float(0x7fc00000);
        float value = (float)source[row * stride_y + sx * stride_x + channel * stride_c];
        float product = __fmul_rn(value, weights[x * 4 + tap]);
        result = tap == 0 ? product : __fadd_rn(result, product);
    }
    return result;
}

extern "C" __global__ void quest3d_reference_cubic(
        const unsigned char *source, float *output, long long source_width,
        long long source_height, long long stride_y, long long stride_x,
        long long stride_c, int width, int height,
        const long long *x_indices, const float *x_weights,
        const long long *y_indices, const float *y_weights) {
    long long element = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    if (element >= 3LL * width * height) return;
    int channel = (int)(element % 3);
    long long x = (element / 3) % width;
    long long y = element / (3LL * width);
    if (!y_indices) {
        output[element] = horizontal(source, y, x, channel, source_width,
            stride_y, stride_x, stride_c, x_indices, x_weights);
        return;
    }
    float result = 0.0f;
    for (int tap = 0; tap < 4; ++tap) {
        long long sy = y_indices[y * 4 + tap];
        if (sy < 0 || sy >= source_height) {
            output[element] = __int_as_float(0x7fc00000);
            return;
        }
        float value = horizontal(source, sy, x, channel, source_width,
            stride_y, stride_x, stride_c, x_indices, x_weights);
        float product = __fmul_rn(value, y_weights[y * 4 + tap]);
        result = tap == 0 ? product : __fadd_rn(result, product);
    }
    output[element] = result;
}
