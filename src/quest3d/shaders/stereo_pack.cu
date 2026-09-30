// Pinned NVRTC 12.6, compute_75, --fmad=false. Input is read-only planar BGR.
extern "C" __global__ void quest3d_pack_stereo(
        const float *eyes, unsigned char *packed, int eye_width, int eye_height,
        int content_x, int content_y, int content_width, int content_height) {
    long long pixel = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long row_width = 2LL * eye_width;
    if (pixel >= row_width * eye_height) return;
    int y = (int)(pixel / row_width);
    int packed_x = (int)(pixel % row_width);
    int eye = packed_x / eye_width;
    int x = packed_x % eye_width;
    unsigned char *output = packed + pixel * 4;
    output[3] = 255;
    if (x < content_x || x >= content_x + content_width
            || y < content_y || y >= content_y + content_height) {
        output[0] = 0;
        output[1] = 0;
        output[2] = 0;
        return;
    }
    long long plane = (long long)content_width * content_height;
    long long offset = (long long)(y - content_y) * content_width + x - content_x;
    for (int channel = 0; channel < 3; ++channel) {
        float value = eyes[(eye * 3 + channel) * plane + offset];
        // Clamp, multiplication and round-to-nearest-even preserve Torch's
        // separate float32 operations; source values are never overwritten.
        float bounded = fminf(fmaxf(value, 0.0f), 1.0f);
        output[channel] = isnan(value) ? 0 : (unsigned char)__float2uint_rn(bounded * 255.0f);
    }
}
