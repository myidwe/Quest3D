// Torch 2.7.1 bicubic-AA axis coefficients are supplied by the Torch oracle.
// Their normalization and edge truncation are never recomputed in this kernel.
// Preserve horizontal float32 sum -> float32 intermediate -> vertical sum.
// Explicit RN intrinsics keep the intended contraction boundaries even though
// NVRTC --fmad=false prevents any additional compiler-introduced contraction.

__device__ __forceinline__ float normalized_colour(float value) {
    // Torch div_(255) uses the CPU scalar's float32 reciprocal then multiply.
    const float reciprocal_255 = __int_as_float(0x3b808081);
    return __fmul_rn(fminf(fmaxf(value, 0.0f), 255.0f), reciprocal_255);
}

extern "C" __global__ void quest3d_colour_same_size(
        const unsigned char *source, float *output,
        int width, int height, long long stride_y, long long stride_x, long long stride_c) {
    long long offset = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)width * height;
    if (offset >= 3 * plane) return;
    int channel = (int)(offset / plane);
    long long pixel = offset % plane;
    int y = (int)(pixel / width), x = (int)(pixel % width);
    float value = (float)source[y * stride_y + x * stride_x + channel * stride_c];
    output[offset] = normalized_colour(value);
}

extern "C" __global__ void quest3d_colour_horizontal(
        const unsigned char *source, float *temporary,
        int input_height, int output_width,
        long long stride_y, long long stride_x, long long stride_c,
        const int *indices, const float *weights, int taps) {
    long long offset = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)input_height * output_width;
    if (offset >= 3 * plane) return;
    int channel = (int)(offset / plane);
    long long pixel = offset % plane;
    int y = (int)(pixel / output_width), x = (int)(pixel % output_width);
    const unsigned char *row = source + y * stride_y + channel * stride_c;
    const int *ix = indices + (long long)x * taps;
    const float *wx = weights + (long long)x * taps;
    float value = __fmul_rn((float)row[(long long)ix[0] * stride_x], wx[0]);
    for (int tap = 1; tap < taps; ++tap) {
        value = __fmaf_rn((float)row[(long long)ix[tap] * stride_x], wx[tap], value);
    }
    temporary[offset] = value;
}

extern "C" __global__ void quest3d_colour_vertical(
        const float *temporary, float *output,
        int input_height, int width, int output_height,
        const int *indices, const float *weights, int taps) {
    long long offset = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)width * output_height;
    if (offset >= 3 * plane) return;
    int channel = (int)(offset / plane);
    long long pixel = offset % plane;
    int y = (int)(pixel / width), x = (int)(pixel % width);
    const float *image = temporary + (long long)channel * input_height * width;
    const int *iy = indices + (long long)y * taps;
    const float *wy = weights + (long long)y * taps;
    float value = __fmul_rn(image[(long long)iy[0] * width + x], wy[0]);
    for (int tap = 1; tap < taps; ++tap) {
        value = __fmaf_rn(image[(long long)iy[tap] * width + x], wy[tap], value);
    }
    output[offset] = normalized_colour(value);
}
