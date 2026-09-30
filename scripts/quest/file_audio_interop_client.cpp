#include "network/file_audio_transport.h"
#include <cassert>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iostream>
#include <thread>
#include <unistd.h>

using namespace nightfall::file_audio;
static std::string read_file(const std::string& path) {
    std::ifstream stream(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(stream), {}};
}

// Own only the real FileAudioTransport and a byte-checking sink, with no fake TLS peer.
int main(int argc, char** argv) {
    assert(argc == 7);
    std::cout << "{\"client_pid\":" << getpid() << "}" << std::endl;
    const std::string mode = argv[5];
    const bool seek = mode == "seek", cancel_mode = mode == "cancel";
    const bool normal = mode == "first" || seek;
    const auto pcm = read_file(argv[6]);
    TransportRequest request;
    request.url = argv[1]; request.server_cert_pem = read_file(argv[2]);
    request.client_cert_pem = read_file(argv[3]); request.client_key_pem = read_file(argv[4]);
    request.scope.file_session.fill(0x11); request.scope.transport.fill(0x22); request.scope.channel.fill(0x33);
    request.scope.epoch = seek ? 8 : 7; request.scope.generation = seek ? 10 : 9;
    request.connect_timeout_ms = 1500; request.idle_timeout_ms = 3000;
    FileAudioTransport transport;
    std::atomic_bool entered{false};
    size_t delivered = 0, frames = 0;
    bool eof = false;
    assert(transport.start(request, [&](const Block& block, const std::atomic_bool& cancel) {
        assert(block.scope == request.scope && block.sequence == delivered);
        if (!block.eof()) {
            assert(frames * 8 + block.count * 8 <= pcm.size());
            assert(std::memcmp(block.samples.data(), pcm.data() + frames * 8, block.count * 8) == 0);
            assert(block.pts_ns == int64_t(delivered * 10000000 + (seek ? 20000000 : 0)));
            assert(block.discontinuity == (delivered == 0 ? (seek ? 2 : 1) : 0));
            if (delivered == 0) {
                std::cout << "{\"first_block\":true,\"frames\":" << block.count << "}" << std::endl;
                entered.store(true);
            }
            if (cancel_mode) {
                const auto until = std::chrono::steady_clock::now() + std::chrono::seconds(5);
                while (!cancel.load()) { assert(std::chrono::steady_clock::now() < until); std::this_thread::yield(); }
                return false;
            }
            frames += block.count;
        } else {
            assert(block.pts_ns == 104229167); eof = true;
        }
        ++delivered;
        return true;
    }));
    const auto until = std::chrono::steady_clock::now() + std::chrono::seconds(8);
    if (cancel_mode) {
        while (!entered.load()) { assert(std::chrono::steady_clock::now() < until); std::this_thread::yield(); }
    } else {
        while (!transport.snapshot().worker_exited) { assert(std::chrono::steady_clock::now() < until); std::this_thread::yield(); }
    }
    assert(transport.stop());
    const auto result = transport.snapshot();
    assert(result.worker_exited && result.joined && !result.callback_active);
    if (normal) {
        assert(result.state == TransportState::completed && eof && result.http_status == 200 && result.curl_code == 0);
        assert(frames == (seek ? 4043 : 5003) && frames * 8 == pcm.size());
        assert(delivered == (seek ? 10 : 12));
    } else if (cancel_mode) {
        assert(result.state == TransportState::cancelled && !eof && delivered == 0);
    } else {
        assert(result.state == TransportState::failed && !eof);
        if (mode == "abort") assert(frames == 480 && result.curl_code != 0);
        else assert(delivered == 0);
    }
    std::cout << "{\"result\":true,\"state\":" << int(result.state) << ",\"error\":" << int(result.error)
              << ",\"http\":" << result.http_status << ",\"curl\":" << result.curl_code << ",\"bytes\":" << result.response_bytes
              << ",\"callbacks\":" << result.body_callbacks << ",\"records\":" << delivered << ",\"frames\":" << frames
              << ",\"eof\":" << (eof ? "true" : "false") << ",\"joined\":true}" << std::endl;
}
