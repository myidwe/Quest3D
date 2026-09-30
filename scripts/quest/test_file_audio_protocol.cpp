#include "audio/file_audio_protocol.h"
#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>

using namespace nightfall::file_audio;

Scope known_scope() {
    Scope scope;
    scope.file_session.fill(0x11); scope.transport.fill(0x22); scope.channel.fill(0x33);
    scope.epoch = 7; scope.generation = 9;
    return scope;
}

Block sample() {
    Block block;
    block.scope = known_scope(); block.block_id = 12; block.pts_ns = -100;
    block.total_samples = 4; block.offset_samples = 1; block.count = 3; block.discontinuity = 2;
    block.samples[0] = .1f; block.samples[1] = -.1f;
    block.samples[2] = .2f; block.samples[3] = -.2f;
    block.samples[4] = .3f; block.samples[5] = -.3f;
    return block;
}

int main(int argc, char **argv) {
    if (argc == 3 || argc == 5) {
        std::ifstream source(argv[1], std::ios::binary);
        assert(source);
        std::vector<uint8_t> wire{std::istreambuf_iterator<char>(source), {}};
        std::ofstream output(argv[2], std::ios::binary);
        assert(output);
        auto expected = known_scope();
        if (argc == 5) {
            expected.epoch = std::stoull(argv[3]);
            expected.generation = std::stoull(argv[4]);
        }
        Decoder decoder(expected);
        uint64_t frames = 0;
        size_t encoded_offset = 0;
        // Real-file vector created by Windows MediaAudioReader, deliberately
        // split inside headers, samples and EOF across calls.
        for (size_t offset = 0; offset < wire.size();) {
            const size_t length = std::min(size_t(1 + offset % 4111), wire.size() - offset);
            assert(decoder.feed(wire.data() + offset, length, [&](const Block &block) {
                const auto encoded = encode(block);
                assert(encoded_offset + encoded.size() <= wire.size());
                assert(std::equal(encoded.begin(), encoded.end(), wire.begin() + encoded_offset));
                encoded_offset += encoded.size();
                if (!block.eof()) output.write(reinterpret_cast<const char *>(block.samples.data()), block.count * 8);
                frames += block.count;
                return true;
            }));
            offset += length;
        }
        assert(decoder.finish() && decoder.eof());
        std::cout << "Decoded actual-file PCM frames=" << frames << " blocks=" << decoder.blocks() << "\n";
        return 0;
    }
    auto first = sample();
    {
        auto invalid = first;
        invalid.count = 0;
        assert(encode(invalid).empty());
    }
    const auto one = encode(first);
    auto last = first;
    last.sequence = 1; last.block_id = 13; last.pts_ns = 62'400;
    last.total_samples = last.offset_samples = last.count = 0; last.flags = FLAG_EOF; last.discontinuity = 0;
    auto wire = one;
    const auto ending = encode(last);
    wire.insert(wire.end(), ending.begin(), ending.end());
    for (size_t chunk : {size_t(1), size_t(7), size_t(127), size_t(128), size_t(3968)}) {
        Decoder decoder(known_scope());
        std::vector<uint8_t> reencoded;
        for (size_t offset = 0; offset < wire.size(); offset += std::min(chunk, wire.size() - offset)) {
            assert(decoder.feed(wire.data() + offset, std::min(chunk, wire.size() - offset), [&](const Block &block) {
                const auto encoded = encode(block);
                reencoded.insert(reencoded.end(), encoded.begin(), encoded.end());
                return true;
            }));
            assert(decoder.buffered_bytes() <= MAX_BLOCK_BYTES);
        }
        assert(decoder.finish() && decoder.blocks() == 2 && reencoded == wire);
    }
    for (const auto &change : std::vector<std::pair<size_t,uint8_t>>{{0,0},{8,2},{10,127},{12,25},{104,0},
             {108,1},{110,4},{112,4},{114,1},{115,2},{116,2},{120,6},{121,1},{127,1},{16,0}}) {
        auto invalid = one; invalid[change.first] = change.second;
        Decoder decoder(known_scope());
        bool invoked = false;
        assert(!decoder.feed(invalid.data(), invalid.size(), [&](const Block &) {invoked=true;return true;}));
        assert(!invoked && !decoder.finish());
    }
    for (size_t cut : {size_t(1), size_t(127), size_t(128), one.size()-1, one.size()}) {
        Decoder decoder(known_scope());
        assert(decoder.feed(one.data(), cut, [](const Block &) {return true;}));
        assert(!decoder.finish()); // Missing EOF also fails after one full PCM record.
    }
    {
        Decoder decoder(known_scope());
        assert(decoder.feed(one.data(), one.size(), [](const Block &) {return true;}));
        assert(!decoder.feed(one.data(), one.size(), [](const Block &) {assert(false);return true;}));
        assert(decoder.error() == "noncontiguous_sequence");
    }
    for (uint32_t bits : {0x7F800000U, 0x7FC00000U, 0xFF800000U}) {
        auto invalid = one;
        detail::write_le(invalid.data() + HEADER_BYTES, bits, 4);
        Decoder decoder(known_scope());
        assert(!decoder.feed(invalid.data(), invalid.size(), [](const Block &) {assert(false);return true;}));
        assert(decoder.error() == "invalid_pcm");
    }
    {
        Decoder decoder(known_scope());
        assert(!decoder.feed(one.data(), one.size(), [](const Block &) {return false;}));
        assert(decoder.error() == "consumer_rejected" && decoder.blocks() == 0);
    }
#if defined(__cpp_exceptions) || defined(_CPPUNWIND)
    {
        Decoder decoder(known_scope());
        assert(!decoder.feed(one.data(), one.size(), [](const Block &) -> bool {throw std::runtime_error("test");}));
        assert(decoder.error() == "consumer_exception");
    }
#endif
    {
        Decoder decoder(known_scope());
        assert(decoder.feed(wire.data(), wire.size(), [](const Block &) {return true;}));
        assert(!decoder.feed(one.data(), 1, [](const Block &) {assert(false);return true;}));
        assert(decoder.error() == "data_after_eof");
    }
    {
        first.total_samples = first.count = MAX_FRAMES; first.offset_samples = 0;
        const auto full = encode(first);
        assert(full.size() == MAX_BLOCK_BYTES);
        Decoder decoder(known_scope());
        assert(decoder.feed(full.data(), full.size(), [](const Block &b) {return b.count == MAX_FRAMES;}));
    }
    std::cout << "PASS bounded file PCM protocol: fragmentation, exact roundtrip, malformed/old/duplicate/EOF/callback failure\n";
}
