#include "curl_http_client.h"
#include "pairing_security.h"
#include "pc_stream_profile.h"
#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>

std::string read(const char* path) {
    std::ifstream file(path);
    return {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
}

void proof_test(const std::string& cert_pem, const std::string& key_pem) {
    auto cert = nightfall::parse_certificate(cert_pem);
    assert(cert);
    BIO* bio = BIO_new_mem_buf(key_pem.data(), static_cast<int>(key_pem.size()));
    auto* key = PEM_read_bio_PrivateKey(bio, nullptr, nullptr, nullptr);
    BIO_free(bio);
    assert(key);
    std::vector<uint8_t> challenge(16, 7), secret(16, 3);
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    assert(EVP_DigestSignInit(ctx, nullptr, EVP_sha256(), nullptr, key) == 1);
    size_t signature_size = 0;
    assert(EVP_DigestSign(ctx, nullptr, &signature_size, secret.data(), secret.size()) == 1);
    std::vector<uint8_t> signature(signature_size);
    assert(EVP_DigestSign(ctx, signature.data(), &signature_size, secret.data(), secret.size()) == 1);
    signature.resize(signature_size);
    EVP_MD_CTX_free(ctx);
    EVP_PKEY_free(key);
    const ASN1_BIT_STRING* certificate_signature = nullptr;
    X509_get0_signature(&certificate_signature, nullptr, cert.get());
    const auto* sig_data = ASN1_STRING_get0_data(certificate_signature);
    std::vector<uint8_t> proof_data(challenge);
    proof_data.insert(proof_data.end(), sig_data, sig_data + ASN1_STRING_length(certificate_signature));
    proof_data.insert(proof_data.end(), secret.begin(), secret.end());
    std::vector<uint8_t> hash(32);
    unsigned int hash_size = 0;
    assert(EVP_Digest(proof_data.data(), proof_data.size(), hash.data(), &hash_size, EVP_sha256(), nullptr) == 1);
    std::vector<uint8_t> payload(secret);
    payload.insert(payload.end(), signature.begin(), signature.end());
    assert(nightfall::verify_pairing_proof(cert_pem, challenge, hash, payload));
    hash[0] ^= 1;
    assert(!nightfall::verify_pairing_proof(cert_pem, challenge, hash, payload));
    hash[0] ^= 1;
    payload[0] ^= 1;
    assert(!nightfall::verify_pairing_proof(cert_pem, challenge, hash, payload));
    payload[0] ^= 1;
    payload.back() ^= 1;
    assert(!nightfall::verify_pairing_proof(cert_pem, challenge, hash, payload));
    assert(!nightfall::verify_pairing_proof("invalid", challenge, hash, payload));
    assert(!nightfall::verify_pairing_proof(cert_pem, {}, hash, payload));
    std::cout << "Pairing proof: valid passes; wrong PIN hash, secret, signature, cert, size fail\n";
}

int main(int argc, char** argv) {
    int dimension = 0;
    for (const auto text : {"2", "720", "2560", "8192"})
        assert(nightfall::parse_pc_dimension(text, dimension));
    for (const auto text : {"", "0", "1", "2561", "-2", "+2", "02560", " 2560", "2560 ", "2560px", "2.0", "8194", "999999999999999999999999999"})
        assert(!nightfall::parse_pc_dimension(text, dimension));
    assert(argc >= 4);
    if (std::string(argv[1]) == "proof") { proof_test(read(argv[2]), read(argv[3])); return 0; }
    assert(argc == 7);
    nightfall::CurlHttpClient client;
    client.set_timeout_ms(2500);
    if (std::string(argv[2]) != "none") client.set_server_cert_pin(read(argv[2]));
    if (std::string(argv[3]) != "none") client.set_client_cert(read(argv[3]), read(argv[4]));
    std::string mode = argv[5];
    if (mode == "insecure") client.set_verify_peer(false);
    if (mode == "inject") {
        assert(!client.set_headers({{"Connection", "close\r\nX-Bad: 1"}}));
        assert(!client.set_headers({{"Authorization", "unexpected"}}));
        std::cout << "Headers rejected\n";
        return 0;
    }
    assert(client.set_headers({{"Accept", "application/json"}, {"Connection", "close"}}));
    nightfall::HttpResponse response;
    if (mode == "post") {
        std::string body = argv[6];
        if (!body.empty() && body.front() == '@') body = read(body.substr(1).c_str());
        response = client.post(argv[1], "application/json", {body.begin(), body.end()});
    } else response = client.get(argv[1]);
    std::cout << response.status_code << '\n' << response.body << '\n';
    return 0;
}
