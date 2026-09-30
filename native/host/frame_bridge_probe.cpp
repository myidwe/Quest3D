// Read real Python-produced frames without capture, network listeners, or input injection.
#include "quest3d_frame.h"
#include <algorithm>
#include <cstdlib>
#include <iostream>
#include <string>
#include <charconv>
#include <bcrypt.h>

std::string payload_sha256(const std::vector<std::uint8_t> &pixels) {
  BCRYPT_ALG_HANDLE algorithm=nullptr;
  if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0) return {};
  std::array<unsigned char,32> digest {};
  const auto status=BCryptHash(algorithm,nullptr,0,const_cast<PUCHAR>(pixels.data()),static_cast<ULONG>(pixels.size()),digest.data(),static_cast<ULONG>(digest.size()));
  BCryptCloseAlgorithmProvider(algorithm,0);
  if(status<0)return {};
  constexpr char alphabet[]="0123456789abcdef";
  std::string result;
  for(auto byte:digest) {result+=alphabet[byte>>4];result+=alphabet[byte&15];}
  return result;
}

int main(int argc, char **argv) {
  int duration_seconds=10;
  unsigned protocol=2;
  bool profile=false;
  bool checksum=false;
  std::wstring prefix=L"Local\\Quest3D.Frame";
  for(int i=1;i<argc;++i) {
    const std::string argument=argv[i];
    if(argument=="--profile") profile=true;
    else if(argument=="--checksum") checksum=true;
    else if(argument=="--protocol"&&i+1<argc) {
      const std::string value=argv[++i];
      if(value!="2"&&value!="3") return 64;
      protocol=value=="3"?3:2;
    } else if(argument=="--prefix"&&i+1<argc) {
      const std::string value=argv[++i];
      if(value.size()>160||!value.starts_with("Local\\")||!std::all_of(value.begin(),value.end(),[](unsigned char c){return c>=32&&c<127;})) return 64;
      prefix.assign(value.begin(),value.end());
    } else if(i==1) {
      const auto parsed=std::from_chars(argument.data(),argument.data()+argument.size(),duration_seconds);
      if(parsed.ec!=std::errc{}||parsed.ptr!=argument.data()+argument.size()||duration_seconds<1||duration_seconds>120) return 64;
    } else return 64;
  }
  const auto names=quest3d::protocol_names(protocol,prefix);
  if (profile) {
    quest3d::frame_reader source(names);
    quest3d::frame_header metadata {};
    if (source.read_metadata(metadata, 100) != quest3d::read_result::fresh ||
        !quest3d::full_sbs_profile(metadata, quest3d::qpc_nanoseconds())) return 2;
    std::cout << "{\"bridge_protocol\":" << protocol << ",\"Quest3DProfileVersion\":1,\"Quest3DLayout\":\"full_sbs\",\"Quest3DWidth\":"
              << metadata.width << ",\"Quest3DHeight\":" << metadata.height << "}\n";
    return 0;
  }
  quest3d::frame_reader reader(names);
  quest3d::frame value;
  const auto deadline = GetTickCount64() + static_cast<ULONGLONG>(duration_seconds) * 1000;
  std::uint64_t frames = 0;
  while (GetTickCount64() < deadline) {
    const auto result = reader.read(value, 100);
    if (result != quest3d::read_result::fresh) {
      Sleep(5);
      continue;
    }
    const auto &h = value.header;
    const auto now = quest3d::qpc_nanoseconds();
    const auto age = now >= h.capture_qpc_ns ? (now - h.capture_qpc_ns) / 1000000.0 : -1.0;
    const auto hash=checksum?payload_sha256(value.pixels):std::string{};
    if(checksum&&hash.empty())return 3;
    std::cout << "{\"bridge_protocol\":" << protocol << ",\"frame_id\":" << h.frame_id << ",\"stream_epoch\":" << h.stream_epoch
              << ",\"width\":" << h.width << ",\"height\":" << h.height
              << ",\"flags\":" << h.flags << ",\"source_age_ms\":" << age
              << ",\"payload_bytes\":" << h.payload_bytes
              << ",\"source_kind\":\"" << quest3d::source_kind_name(value.source.kind)
              << "\",\"source_selection_id\":\"" << value.source.selection_id
              << "\",\"source_native_handle\":\"" << value.source.native_handle
              << "\",\"source_creation_filetime\":\"" << value.source.creation_filetime
              << "\",\"source_process_id\":" << value.source.process_id
              << ",\"parent_bounds\":[" << value.source.parent_left << ',' << value.source.parent_top << ',' << value.source.parent_width << ',' << value.source.parent_height << ']';
    if(checksum)std::cout<<",\"payload_sha256\":\""<<hash<<'"';
    std::cout<<"}\n";
    frames++;
  }
  std::cerr << "frames_read=" << frames << '\n';
  return frames > 0 ? 0 : 2;
}
