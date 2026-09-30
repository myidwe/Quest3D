// Pinned NVRTC 12.6, compute_75, --fmad=false. Reference: forward_warp.py.
// No atomic colour/depth scatter and no fixed-size contributor array. Each
// destination thread scans its bounded horizontal source neighbourhood.
// NVRTC supplies device math intrinsics without an external toolkit include
// search path. Match the existing pinned tone mapper's header-free compilation.
#define Q3D_INFINITY __int_as_float(0x7f800000)

// Optional validation of the same contiguous copies used for projection.
// No device assertions: the caller receives the completed invalid flag and
// raises an ordinary ValueError before launching any projection/fill work.
extern "C" __global__ void quest3d_forward_validate_values(
        const float *image, const float *depth, int *invalid_flag, int count,
        int image_negative, int depth_negative, float projection_depth_low,
        float projection_depth_high) {
    bool invalid = false;
    int step = (int)(gridDim.x * blockDim.x);
    for (int pixel = (int)(blockIdx.x * blockDim.x + threadIdx.x);
            pixel < count; pixel += step) {
        float b = image[pixel];
        float g = image[pixel + count];
        float r = image[pixel + 2 * count];
        float z = depth[pixel];
        // A contiguous Torch view can still carry a lazy negative bit. Check
        // its logical values, as Torch predicates do, without mutating inputs.
        if (image_negative) { b = -b; g = -g; r = -r; }
        if (depth_negative) z = -z;
        invalid = invalid || !isfinite(b) || b < 0.0f || b > 1.0f
            || !isfinite(g) || g < 0.0f || g > 1.0f
            || !isfinite(r) || r < 0.0f || r > 1.0f
            || !isfinite(z) || z < projection_depth_low || z > projection_depth_high;
    }
    // A valid frame has no global atomic updates. For an invalid frame, reduce
    // per block and OR one flag; every input is still visited before returning.
    if (__syncthreads_or(invalid) && threadIdx.x == 0) atomicOr(invalid_flag, 1);
}

static const float COVERAGE_EPS = 1.0e-6f;

__device__ bool footprint(int source_x, int destination_x, float depth,
                         float sign, float half_disparity, float convergence,
                         float *begin, float *end) {
    // Preserve the separate float32 operations used by the Torch reference.
    float projected = (float)source_x + (sign * (depth - convergence)) * half_disparity;
    if (!isfinite(projected) || projected < (float)destination_x - 1.0f
            || projected > (float)destination_x + 1.0f) return false;
    float lower = floorf(projected);
    float fraction = projected - lower;
    if (lower == (float)destination_x) {
        *begin = fraction;
        *end = 1.0f;
    } else if (lower + 1.0f == (float)destination_x) {
        *begin = 0.0f;
        *end = fraction;
    } else {
        return false;
    }
    return *end - *begin > COVERAGE_EPS;
}

extern "C" __global__ void quest3d_forward_project(
        const float *image, const float *depth, float *raw_colour,
        float *remaining, float *nearest, float *farthest, unsigned char *holes,
        int width, int height, float half_disparity, float convergence,
        int max_layers, float projection_depth_low, float projection_depth_high) {
    long long pixel = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)width * height;
    if (pixel >= 2 * plane) return;
    int eye = (int)(pixel / plane);
    int y = (int)((pixel % plane) / width);
    int x = (int)(pixel % width);
    float sign = eye == 0 ? 1.0f : -1.0f;
    // Host validation proves all input depths lie within this interval. Only
    // the conservative candidate search changes; projection and fill are exact.
    float endpoint_a = (sign * (projection_depth_low - convergence)) * half_disparity;
    float endpoint_b = (sign * (projection_depth_high - convergence)) * half_disparity;
    float min_shift = fminf(endpoint_a, endpoint_b);
    float max_shift = fmaxf(endpoint_a, endpoint_b);
    // Conservative bounds include zero-area endpoint candidates, which the
    // footprint/remaining-overlap checks discard. Clamp before float->int.
    int source_begin = (int)fminf((float)width,
        fmaxf(0.0f, floorf((float)x - max_shift - 1.0f)));
    int source_end = (int)fmaxf(-1.0f,
        fminf((float)(width - 1), ceilf((float)x - min_shift + 1.0f)));
    float lo = 0.0f, hi = 1.0f;
    float ceiling = Q3D_INFINITY, near_value = -Q3D_INFINITY, far_value = Q3D_INFINITY;
    float red = 0.0f, green = 0.0f, blue = 0.0f;
    long long source_row = (long long)y * width;

    for (int layer = 0; layer < max_layers && hi - lo > COVERAGE_EPS; ++layer) {
        float z = -Q3D_INFINITY;
        for (int sx = source_begin; sx <= source_end; ++sx) {
            float d = depth[source_row + sx], a, b;
            if (!(d < ceiling) || !footprint(sx, x, d, sign, half_disparity,
                                            convergence, &a, &b)) continue;
            float overlap = fmaxf(0.0f, fminf(b, hi) - fmaxf(a, lo));
            if (overlap > COVERAGE_EPS) z = fmaxf(z, d);
        }
        if (!isfinite(z)) break;
        float prefix_end = 0.0f, suffix_begin = 1.0f;
        float weight = 0.0f, r = 0.0f, g = 0.0f, b_colour = 0.0f;
        for (int sx = source_begin; sx <= source_end; ++sx) {
            float d = depth[source_row + sx], a, b;
            if (!(d < ceiling && d == z)
                    || !footprint(sx, x, d, sign, half_disparity,
                                  convergence, &a, &b)) continue;
            float overlap = fmaxf(0.0f, fminf(b, hi) - fmaxf(a, lo));
            if (overlap <= COVERAGE_EPS) continue;
            weight += overlap;
            r += image[source_row + sx] * overlap;
            g += image[plane + source_row + sx] * overlap;
            b_colour += image[2 * plane + source_row + sx] * overlap;
            if (a == 0.0f) prefix_end = fmaxf(prefix_end, b);
            if (b == 1.0f) suffix_begin = fminf(suffix_begin, a);
        }
        float next_lo = fminf(hi, fmaxf(lo, prefix_end));
        float next_hi = fmaxf(next_lo, fminf(hi, suffix_begin));
        float covered = fmaxf(0.0f, (hi - lo) - (next_hi - next_lo));
        float divisor = fmaxf(weight, COVERAGE_EPS);
        red += (r / divisor) * covered;
        green += (g / divisor) * covered;
        blue += (b_colour / divisor) * covered;
        if (covered > COVERAGE_EPS) {
            near_value = fmaxf(near_value, z);
            far_value = fminf(far_value, z);
        }
        lo = next_lo;
        hi = next_hi;
        // Remaining intervals only shrink. A formerly ineligible contributor
        // cannot become eligible later, so this scalar ceiling exactly replaces
        // the reference's per-contributor active mask.
        // Only this exact depth was resolved. Hole-donor similarity tolerance
        // must not merge projected surfaces or introduce a visibility threshold.
        ceiling = z;
    }
    long long output = (long long)eye * 3 * plane + (long long)y * width + x;
    raw_colour[output] = red;
    raw_colour[output + plane] = green;
    raw_colour[output + 2 * plane] = blue;
    float missing = fminf(1.0f, fmaxf(0.0f, hi - lo));
    remaining[pixel] = missing;
    nearest[pixel] = near_value;
    farthest[pixel] = far_value;
    holes[pixel] = missing > COVERAGE_EPS;
}

__device__ bool eligible_donor(long long p, const unsigned char *holes,
                               const float *nearest, const float *farthest,
                               float donor_depth_tolerance) {
    return !holes[p] && isfinite(nearest[p]) && isfinite(farthest[p])
        && nearest[p] - farthest[p] <= donor_depth_tolerance;
}

extern "C" __global__ void quest3d_forward_donor_bounds(
        const unsigned char *holes, const float *nearest, const float *farthest,
        int *bounds, int width, int height, float donor_depth_tolerance) {
    int row = (int)blockIdx.x;
    if (row >= 2 * height) return;
    __shared__ int first[256];
    __shared__ int last[256];
    int t = (int)threadIdx.x;
    int local_first = width, local_last = -1;
    long long base = (long long)row * width;
    for (int x = t; x < width; x += 256) {
        if (eligible_donor(base + x, holes, nearest, farthest, donor_depth_tolerance)) {
            local_first = min(local_first, x);
            local_last = max(local_last, x);
        }
    }
    first[t] = local_first;
    last[t] = local_last;
    __syncthreads();
    for (int step = 128; step > 0; step >>= 1) {
        if (t < step) {
            first[t] = min(first[t], first[t + step]);
            last[t] = max(last[t], last[t + step]);
        }
        __syncthreads();
    }
    if (t == 0) {
        bounds[2 * row] = first[0];
        bounds[2 * row + 1] = last[0];
    }
}

extern "C" __global__ void quest3d_forward_fill(
        const float *raw_colour, const float *remaining, const float *nearest,
        const float *farthest, const unsigned char *holes, const int *bounds,
        float *output_colour, unsigned char *filled, unsigned char *reconstructed,
        int width, int height,
        float donor_depth_tolerance, int max_distance) {
    long long pixel = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)width * height;
    if (pixel >= 2 * plane) return;
    int eye = (int)(pixel / plane);
    int row = (int)(pixel / width);
    int x = (int)(pixel % width);
    long long base = (long long)row * width;
    int selected = -1;
    if (holes[pixel]) {
        int left = -1, right = -1;
        for (int distance = 1; distance <= max_distance; ++distance) {
            if (left < 0 && x - distance >= 0
                    && eligible_donor(base + x - distance, holes, nearest,
                                      farthest, donor_depth_tolerance)) left = x - distance;
            if (right < 0 && x + distance < width
                    && eligible_donor(base + x + distance, holes, nearest,
                                      farthest, donor_depth_tolerance)) right = x + distance;
            if (left >= 0 && right >= 0) break;
        }
        bool left_exists = bounds[2 * row] <= x;
        bool right_exists = bounds[2 * row + 1] >= x;
        bool both_bounded = left >= 0 && right >= 0;
        bool exposed_edge = (left >= 0 && !right_exists) || (right >= 0 && !left_exists);
        if (both_bounded || exposed_edge) {
            if (left >= 0 && right >= 0) {
                float ld = nearest[base + left], rd = nearest[base + right];
                bool prefer_left = ld < rd - donor_depth_tolerance
                    || (fabsf(ld - rd) <= donor_depth_tolerance && x - left <= right - x);
                selected = prefer_left ? left : right;
            } else {
                selected = left >= 0 ? left : right;
            }
            if (isfinite(nearest[pixel])
                    && nearest[base + selected] > nearest[pixel] + donor_depth_tolerance) selected = -1;
        }
    }
    float coverage = 1.0f - remaining[pixel];
    bool reconstruct = holes[pixel] && selected < 0 && coverage > COVERAGE_EPS;
    reconstructed[pixel] = reconstruct;
    filled[pixel] = selected >= 0 || reconstruct;
    long long offset = (long long)eye * 3 * plane + (pixel % plane);
    long long donor_offset = (long long)eye * 3 * plane + (pixel % plane) - x + selected;
    for (int channel = 0; channel < 3; ++channel) {
        float value = raw_colour[offset + channel * plane];
        if (selected >= 0) value += raw_colour[donor_offset + channel * plane] * remaining[pixel];
        // Only unresolved partial pixels normalize their actual observed RGB.
        // Keep the raw hole mask; this does not recover hidden background.
        else if (reconstruct) value /= fmaxf(coverage, COVERAGE_EPS);
        output_colour[offset + channel * plane] = fminf(1.0f, fmaxf(0.0f, value));
    }
}

__device__ bool eligible_estimated_donor(long long p, const float *remaining,
                                        const float *nearest, const float *farthest,
                                        float donor_depth_tolerance) {
    return 1.0f - remaining[p] > COVERAGE_EPS
        && isfinite(nearest[p]) && isfinite(farthest[p])
        && nearest[p] - farthest[p] <= donor_depth_tolerance;
}

extern "C" __global__ void quest3d_forward_estimated_donor_bounds(
        const float *remaining, const float *nearest, const float *farthest,
        int *bounds, int width, int height, float donor_depth_tolerance) {
    int row = (int)blockIdx.x;
    if (row >= 2 * height) return;
    __shared__ int first[256];
    __shared__ int last[256];
    int t = (int)threadIdx.x;
    int local_first = width, local_last = -1;
    long long base = (long long)row * width;
    for (int x = t; x < width; x += 256) {
        if (eligible_estimated_donor(base + x, remaining, nearest, farthest, donor_depth_tolerance)) {
            local_first = min(local_first, x);
            local_last = max(local_last, x);
        }
    }
    first[t] = local_first;
    last[t] = local_last;
    __syncthreads();
    for (int step = 128; step > 0; step >>= 1) {
        if (t < step) {
            first[t] = min(first[t], first[t + step]);
            last[t] = max(last[t], last[t + step]);
        }
        __syncthreads();
    }
    if (t == 0) {
        bounds[2 * row] = first[0];
        bounds[2 * row + 1] = last[0];
    }
}

extern "C" __global__ void quest3d_forward_estimated_donor_fill(
        const float *raw_colour, const float *remaining, const float *nearest,
        const float *farthest, const unsigned char *holes, const int *bounds,
        float *output_colour, unsigned char *filled, unsigned char *estimated,
        int width, int height, float donor_depth_tolerance, int max_distance) {
    long long pixel = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)width * height;
    if (pixel >= 2 * plane) return;
    estimated[pixel] = 0;
    // Preserve primary donor priority and the distinct partial-pixel mask. A
    // complete pixel cannot normalize itself: only another original observation
    // may supply colour, and no earlier filled/reconstructed output is a donor.
    if (!holes[pixel] || filled[pixel] || 1.0f - remaining[pixel] > COVERAGE_EPS) return;
    int eye = (int)(pixel / plane);
    int row = (int)(pixel / width);
    int x = (int)(pixel % width);
    long long base = (long long)row * width;
    int left = -1, right = -1;
    for (int distance = 1; distance <= max_distance; ++distance) {
        if (left < 0 && x - distance >= 0
                && eligible_estimated_donor(base + x - distance, remaining, nearest,
                                             farthest, donor_depth_tolerance)) left = x - distance;
        if (right < 0 && x + distance < width
                && eligible_estimated_donor(base + x + distance, remaining, nearest,
                                             farthest, donor_depth_tolerance)) right = x + distance;
        if (left >= 0 && right >= 0) break;
    }
    bool left_exists = bounds[2 * row] <= x;
    bool right_exists = bounds[2 * row + 1] >= x;
    bool both_bounded = left >= 0 && right >= 0;
    bool exposed_edge = (left >= 0 && !right_exists) || (right >= 0 && !left_exists);
    if (!both_bounded && !exposed_edge) return;
    int selected;
    if (left >= 0 && right >= 0) {
        float ld = nearest[base + left], rd = nearest[base + right];
        bool prefer_left = ld < rd - donor_depth_tolerance
            || (fabsf(ld - rd) <= donor_depth_tolerance && x - left <= right - x);
        selected = prefer_left ? left : right;
    } else {
        selected = left >= 0 ? left : right;
    }
    if (isfinite(nearest[pixel])
            && nearest[base + selected] > nearest[pixel] + donor_depth_tolerance) return;
    long long output = (long long)eye * 3 * plane + (pixel % plane);
    long long donor = output - x + selected;
    float observed_coverage = fmaxf(COVERAGE_EPS, 1.0f - remaining[base + selected]);
    for (int channel = 0; channel < 3; ++channel) {
        float value = output_colour[output + channel * plane]
            + (raw_colour[donor + channel * plane] / observed_coverage) * remaining[pixel];
        output_colour[output + channel * plane] = fminf(1.0f, fmaxf(0.0f, value));
    }
    filled[pixel] = 1;
    estimated[pixel] = 1;
}

// Explicit fused candidate. Both donor classes depend only on the original
// projection, so their row bounds can be reduced together without promoting a
// repaired pixel into a donor. Layout is [primary first,last, estimated first,last].
extern "C" __global__ void quest3d_forward_all_donor_bounds(
        const unsigned char *holes, const float *remaining, const float *nearest,
        const float *farthest, int *bounds, int width, int height,
        float donor_depth_tolerance) {
    int row = (int)blockIdx.x;
    if (row >= 2 * height) return;
    __shared__ int first[256], last[256], estimated_first[256], estimated_last[256];
    int t = (int)threadIdx.x;
    int local_first = width, local_last = -1;
    int local_estimated_first = width, local_estimated_last = -1;
    long long base = (long long)row * width;
    for (int x = t; x < width; x += 256) {
        if (eligible_donor(base + x, holes, nearest, farthest, donor_depth_tolerance)) {
            local_first = min(local_first, x);
            local_last = max(local_last, x);
        }
        if (eligible_estimated_donor(base + x, remaining, nearest, farthest, donor_depth_tolerance)) {
            local_estimated_first = min(local_estimated_first, x);
            local_estimated_last = max(local_estimated_last, x);
        }
    }
    first[t] = local_first;
    last[t] = local_last;
    estimated_first[t] = local_estimated_first;
    estimated_last[t] = local_estimated_last;
    __syncthreads();
    for (int step = 128; step > 0; step >>= 1) {
        if (t < step) {
            first[t] = min(first[t], first[t + step]);
            last[t] = max(last[t], last[t + step]);
            estimated_first[t] = min(estimated_first[t], estimated_first[t + step]);
            estimated_last[t] = max(estimated_last[t], estimated_last[t + step]);
        }
        __syncthreads();
    }
    if (t == 0) {
        bounds[4 * row] = first[0];
        bounds[4 * row + 1] = last[0];
        bounds[4 * row + 2] = estimated_first[0];
        bounds[4 * row + 3] = estimated_last[0];
    }
}

extern "C" __global__ void quest3d_forward_all_fill(
        const float *raw_colour, const float *remaining, const float *nearest,
        const float *farthest, const unsigned char *holes, const int *bounds,
        float *output_colour, unsigned char *filled, unsigned char *reconstructed,
        unsigned char *estimated, int width, int height,
        float donor_depth_tolerance, int max_distance) {
    long long pixel = (long long)blockIdx.x * blockDim.x + threadIdx.x;
    long long plane = (long long)width * height;
    if (pixel >= 2 * plane) return;
    int eye = (int)(pixel / plane);
    int row = (int)(pixel / width);
    int x = (int)(pixel % width);
    long long base = (long long)row * width;
    int selected = -1;
    if (holes[pixel]) {
        int left = -1, right = -1;
        for (int distance = 1; distance <= max_distance; ++distance) {
            if (left < 0 && x - distance >= 0
                    && eligible_donor(base + x - distance, holes, nearest,
                                      farthest, donor_depth_tolerance)) left = x - distance;
            if (right < 0 && x + distance < width
                    && eligible_donor(base + x + distance, holes, nearest,
                                      farthest, donor_depth_tolerance)) right = x + distance;
            if (left >= 0 && right >= 0) break;
        }
        bool left_exists = bounds[4 * row] <= x;
        bool right_exists = bounds[4 * row + 1] >= x;
        bool both_bounded = left >= 0 && right >= 0;
        bool exposed_edge = (left >= 0 && !right_exists) || (right >= 0 && !left_exists);
        if (both_bounded || exposed_edge) {
            if (left >= 0 && right >= 0) {
                float ld = nearest[base + left], rd = nearest[base + right];
                bool prefer_left = ld < rd - donor_depth_tolerance
                    || (fabsf(ld - rd) <= donor_depth_tolerance && x - left <= right - x);
                selected = prefer_left ? left : right;
            } else {
                selected = left >= 0 ? left : right;
            }
            if (isfinite(nearest[pixel])
                    && nearest[base + selected] > nearest[pixel] + donor_depth_tolerance) selected = -1;
        }
    }
    float coverage = 1.0f - remaining[pixel];
    bool reconstruct = holes[pixel] && selected < 0 && coverage > COVERAGE_EPS;
    bool primary_filled = selected >= 0 || reconstruct;
    reconstructed[pixel] = reconstruct;
    int secondary = -1;
    // The original second kernel only observes this pixel's primary-filled
    // flag. No neighbouring repaired output is read or becomes a donor.
    if (holes[pixel] && !primary_filled && !(coverage > COVERAGE_EPS)) {
        int left = -1, right = -1;
        for (int distance = 1; distance <= max_distance; ++distance) {
            if (left < 0 && x - distance >= 0
                    && eligible_estimated_donor(base + x - distance, remaining, nearest,
                                                 farthest, donor_depth_tolerance)) left = x - distance;
            if (right < 0 && x + distance < width
                    && eligible_estimated_donor(base + x + distance, remaining, nearest,
                                                 farthest, donor_depth_tolerance)) right = x + distance;
            if (left >= 0 && right >= 0) break;
        }
        bool left_exists = bounds[4 * row + 2] <= x;
        bool right_exists = bounds[4 * row + 3] >= x;
        bool both_bounded = left >= 0 && right >= 0;
        bool exposed_edge = (left >= 0 && !right_exists) || (right >= 0 && !left_exists);
        if (both_bounded || exposed_edge) {
            if (left >= 0 && right >= 0) {
                float ld = nearest[base + left], rd = nearest[base + right];
                bool prefer_left = ld < rd - donor_depth_tolerance
                    || (fabsf(ld - rd) <= donor_depth_tolerance && x - left <= right - x);
                secondary = prefer_left ? left : right;
            } else {
                secondary = left >= 0 ? left : right;
            }
            if (isfinite(nearest[pixel])
                    && nearest[base + secondary] > nearest[pixel] + donor_depth_tolerance) secondary = -1;
        }
    }
    filled[pixel] = primary_filled || secondary >= 0;
    estimated[pixel] = secondary >= 0;
    long long offset = (long long)eye * 3 * plane + (pixel % plane);
    long long donor_offset = offset - x + selected;
    long long secondary_offset = offset - x + secondary;
    for (int channel = 0; channel < 3; ++channel) {
        float value = raw_colour[offset + channel * plane];
        if (selected >= 0) value += raw_colour[donor_offset + channel * plane] * remaining[pixel];
        else if (reconstruct) value /= fmaxf(coverage, COVERAGE_EPS);
        // Preserve the primary kernel's float32 clamp before the secondary
        // expression, even though the intermediate value stays in a register.
        value = fminf(1.0f, fmaxf(0.0f, value));
        if (secondary >= 0) {
            float observed_coverage = fmaxf(COVERAGE_EPS, 1.0f - remaining[base + secondary]);
            value = value + (raw_colour[secondary_offset + channel * plane] / observed_coverage) * remaining[pixel];
            value = fminf(1.0f, fmaxf(0.0f, value));
        }
        output_colour[offset + channel * plane] = value;
    }
}

// Fused fill/packing candidate. The original float-eye entry points above stay
// available as the independent numerical reference.
__device__ unsigned char quest3d_pack_channel(float value) {
    float bounded = fminf(fmaxf(value, 0.0f), 1.0f);
    return isnan(value) ? 0 : (unsigned char)__float2uint_rn(__fmul_rn(bounded, 255.0f));
}

extern "C" __global__ void quest3d_forward_zero_packed(
        const float *image, unsigned char *packed, unsigned char *holes,
        unsigned char *filled, unsigned char *reconstructed, unsigned char *estimated,
        int width, int height, int eye_width, int eye_height, int content_x, int content_y) {
    int x = (int)(blockIdx.x * blockDim.x + threadIdx.x);
    int y = (int)(blockIdx.y * blockDim.y + threadIdx.y);
    int eye = (int)blockIdx.z;
    if (x >= eye_width || y >= eye_height || eye >= 2) return;
    long long packed_pixel = (long long)y * (2LL * eye_width) + (long long)eye * eye_width + x;
    unsigned char *destination = packed + packed_pixel * 4;
    destination[3] = 255;
    if (x < content_x || x >= content_x + width || y < content_y || y >= content_y + height) {
        destination[0] = destination[1] = destination[2] = 0;
        return;
    }
    long long plane = (long long)width * height;
    long long offset = (long long)(y - content_y) * width + x - content_x;
    long long pixel = (long long)eye * plane + offset;
    holes[pixel] = filled[pixel] = reconstructed[pixel] = estimated[pixel] = 0;
    for (int channel = 0; channel < 3; ++channel) {
        destination[channel] = quest3d_pack_channel(image[offset + channel * plane]);
    }
}

extern "C" __global__ void quest3d_forward_all_fill_packed(
        const float *raw_colour, const float *remaining, const float *nearest,
        const float *farthest, const unsigned char *holes, const int *bounds,
        unsigned char *packed, unsigned char *filled, unsigned char *reconstructed,
        unsigned char *estimated, int width, int height,
        int eye_width, int eye_height, int content_x, int content_y,
        float donor_depth_tolerance, int max_distance) {
    // An explicit eye axis and 32x8 tiles avoid all inverse pixel indexing.
    // Every warp still accesses consecutive horizontal pixels in one eye.
    int local_x = (int)(blockIdx.x * blockDim.x + threadIdx.x);
    int packed_y = (int)(blockIdx.y * blockDim.y + threadIdx.y);
    int eye = (int)blockIdx.z;
    if (local_x >= eye_width || packed_y >= eye_height || eye >= 2) return;
    long long packed_pixel = (long long)packed_y * (2LL * eye_width)
        + (long long)eye * eye_width + local_x;
    unsigned char *destination = packed + packed_pixel * 4;
    destination[3] = 255;
    if (local_x < content_x || local_x >= content_x + width
            || packed_y < content_y || packed_y >= content_y + height) {
        destination[0] = destination[1] = destination[2] = 0;
        return;
    }
    long long plane = (long long)width * height;
    int x = local_x - content_x;
    int row = eye * height + packed_y - content_y;
    long long content_offset = (long long)(packed_y - content_y) * width + x;
    long long pixel = (long long)eye * plane + content_offset;
    long long base = (long long)row * width;
    int selected = -1;
    if (holes[pixel]) {
        int left = -1, right = -1;
        for (int distance = 1; distance <= max_distance; ++distance) {
            if (left < 0 && x - distance >= 0
                    && eligible_donor(base + x - distance, holes, nearest,
                                      farthest, donor_depth_tolerance)) left = x - distance;
            if (right < 0 && x + distance < width
                    && eligible_donor(base + x + distance, holes, nearest,
                                      farthest, donor_depth_tolerance)) right = x + distance;
            if (left >= 0 && right >= 0) break;
        }
        bool left_exists = bounds[4 * row] <= x;
        bool right_exists = bounds[4 * row + 1] >= x;
        bool both_bounded = left >= 0 && right >= 0;
        bool exposed_edge = (left >= 0 && !right_exists) || (right >= 0 && !left_exists);
        if (both_bounded || exposed_edge) {
            if (left >= 0 && right >= 0) {
                float ld = nearest[base + left], rd = nearest[base + right];
                bool prefer_left = ld < rd - donor_depth_tolerance
                    || (fabsf(ld - rd) <= donor_depth_tolerance && x - left <= right - x);
                selected = prefer_left ? left : right;
            } else {
                selected = left >= 0 ? left : right;
            }
            if (isfinite(nearest[pixel])
                    && nearest[base + selected] > nearest[pixel] + donor_depth_tolerance) selected = -1;
        }
    }
    float coverage = 1.0f - remaining[pixel];
    bool reconstruct = holes[pixel] && selected < 0 && coverage > COVERAGE_EPS;
    bool primary_filled = selected >= 0 || reconstruct;
    reconstructed[pixel] = reconstruct;
    int secondary = -1;
    // The original second kernel only observes this pixel's primary-filled
    // flag. No neighbouring repaired output is read or becomes a donor.
    if (holes[pixel] && !primary_filled && !(coverage > COVERAGE_EPS)) {
        int left = -1, right = -1;
        for (int distance = 1; distance <= max_distance; ++distance) {
            if (left < 0 && x - distance >= 0
                    && eligible_estimated_donor(base + x - distance, remaining, nearest,
                                                 farthest, donor_depth_tolerance)) left = x - distance;
            if (right < 0 && x + distance < width
                    && eligible_estimated_donor(base + x + distance, remaining, nearest,
                                                 farthest, donor_depth_tolerance)) right = x + distance;
            if (left >= 0 && right >= 0) break;
        }
        bool left_exists = bounds[4 * row + 2] <= x;
        bool right_exists = bounds[4 * row + 3] >= x;
        bool both_bounded = left >= 0 && right >= 0;
        bool exposed_edge = (left >= 0 && !right_exists) || (right >= 0 && !left_exists);
        if (both_bounded || exposed_edge) {
            if (left >= 0 && right >= 0) {
                float ld = nearest[base + left], rd = nearest[base + right];
                bool prefer_left = ld < rd - donor_depth_tolerance
                    || (fabsf(ld - rd) <= donor_depth_tolerance && x - left <= right - x);
                secondary = prefer_left ? left : right;
            } else {
                secondary = left >= 0 ? left : right;
            }
            if (isfinite(nearest[pixel])
                    && nearest[base + secondary] > nearest[pixel] + donor_depth_tolerance) secondary = -1;
        }
    }
    filled[pixel] = primary_filled || secondary >= 0;
    estimated[pixel] = secondary >= 0;
    long long offset = (long long)eye * 3 * plane + content_offset;
    long long donor_offset = offset - x + selected;
    long long secondary_offset = offset - x + secondary;
    for (int channel = 0; channel < 3; ++channel) {
        float value = raw_colour[offset + channel * plane];
        if (selected >= 0) value += raw_colour[donor_offset + channel * plane] * remaining[pixel];
        else if (reconstruct) value /= fmaxf(coverage, COVERAGE_EPS);
        // Preserve the primary kernel's float32 clamp before the secondary
        // expression, even though the intermediate value stays in a register.
        value = fminf(1.0f, fmaxf(0.0f, value));
        if (secondary >= 0) {
            float observed_coverage = fmaxf(COVERAGE_EPS, 1.0f - remaining[base + secondary]);
            value = value + (raw_colour[secondary_offset + channel * plane] / observed_coverage) * remaining[pixel];
            value = fminf(1.0f, fmaxf(0.0f, value));
        }
        destination[channel] = quest3d_pack_channel(value);
    }
}
