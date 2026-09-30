// Same fixed scRGB -> SDR policy as tonemap.py. No fast-math or temporal state.
__device__ float from_half(unsigned short bits) {
    float value;
    asm("cvt.f32.f16 %0, %1;" : "=f"(value) : "h"(bits));
    return value;
}

__device__ float normalized(unsigned short bits, float white) {
    float value = from_half(bits);
    if (isnan(value)) value = 0.0f;
    if (isinf(value)) value = value > 0.0f ? 65504.0f : 0.0f;
    return fmaxf(0.0f, value) / white;
}

__device__ unsigned char srgb_code(float value) {
    float encoded = value <= .0031308f ? value * 12.92f
        : 1.055f * powf(fmaxf(value, 0.0f), 1.0f / 2.4f) - .055f;
    return (unsigned char)__float2int_rn(fminf(255.0f, fmaxf(0.0f, encoded * 255.0f)));
}

extern "C" __global__ void quest3d_tone_map(
    const unsigned short* source, unsigned char* output,
    unsigned int width, unsigned int height,
    unsigned long long row_stride, unsigned long long pixel_stride,
    unsigned long long channel_stride, float white, float knee) {
    unsigned int index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index >= width * height) return;
    unsigned int x = index % width, y = index / width;
    const unsigned short* pixel = source + y * row_stride + x * pixel_stride;
    float r = normalized(pixel[0], white);
    float g = normalized(pixel[channel_stride], white);
    float b = normalized(pixel[2 * channel_stride], white);
    float peak = fmaxf(r, fmaxf(g, b));
    float distance = fmaxf(0.0f, peak - knee);
    float mapped = peak <= knee ? peak : knee + (1.0f - knee) * distance / (distance + 1.0f - knee);
    float gain = mapped / fmaxf(peak, 1e-8f);
    output[index * 4 + 0] = srgb_code(b * gain);
    output[index * 4 + 1] = srgb_code(g * gain);
    output[index * 4 + 2] = srgb_code(r * gain);
    output[index * 4 + 3] = 255;
}
