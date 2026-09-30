// Actual isolated Windows mappings: explicit protocol, immutable source and writer death.
#include "quest3d_frame.h"
#include <atomic>
#include <cstring>
#include <future>
#include <iostream>
#include <stdexcept>
#include <thread>

void check_v3(bool value, const char *label) { if (!value) throw std::runtime_error(label); }

struct source_publisher {
  quest3d::object_names names;
  HANDLE mapping=nullptr, mutex=nullptr, ready=nullptr, owner=nullptr;
  std::uint8_t *view=nullptr;
  std::atomic_bool finish=false;
  std::thread owner_thread;
  source_publisher() {
    names=quest3d::protocol_names(3);
    auto prefix=L"Local\\Quest3D.V3Test."+std::to_wstring(GetCurrentProcessId());
    names.mapping=prefix+L".Frame";
    names.mutex=prefix+L".Mutex";
    names.ready=prefix+L".Ready";
    names.producer=prefix+L".Producer";
    mapping=CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,static_cast<DWORD>(quest3d::mapping_capacity+64),names.mapping.c_str());
    mutex=CreateMutexW(nullptr,FALSE,names.mutex.c_str());
    ready=CreateEventW(nullptr,FALSE,FALSE,names.ready.c_str());
    owner=CreateMutexW(nullptr,FALSE,names.producer.c_str());
    check_v3(mapping&&mutex&&ready&&owner,"create v3 objects");
    view=static_cast<std::uint8_t *>(MapViewOfFile(mapping,FILE_MAP_WRITE,0,0,quest3d::mapping_capacity+64));
    check_v3(view,"map v3 view");
    std::promise<void> started;
    auto ready_future=started.get_future();
    owner_thread=std::thread([&] { WaitForSingleObject(owner,INFINITE); started.set_value(); while(!finish) Sleep(1); });
    ready_future.get();
  }
  void die() { finish=true; if(owner_thread.joinable()) owner_thread.join(); }
  ~source_publisher() { die(); if(view)UnmapViewOfFile(view); for(auto h:{mapping,mutex,ready,owner}) if(h)CloseHandle(h); }
  void put(const quest3d::source_frame_header &h) {
    check_v3(WaitForSingleObject(mutex,100)==WAIT_OBJECT_0,"lock v3 publication");
    std::memcpy(view,&h,sizeof(h));
    std::memset(view+192,0x79,h.prefix.payload_bytes);
    ReleaseMutex(mutex); SetEvent(ready);
  }
};

int main() {
  try {
    quest3d::source_frame_header h {};
    auto &p=h.prefix;
    p.magic={'Q','3','D','F','R','M','3','\0'}; p.version=3; p.header_bytes=192;
    p.width=64; p.height=32; p.stride=256; p.payload_bytes=8192; p.format=1;
    p.frame_id=1; p.stream_epoch=9; p.flags=3; p.source_width=100; p.source_height=100;
    p.source_left=-100; p.source_top=20; p.content_width=32; p.content_height=32;
    p.capture_qpc_ns=p.publish_qpc_ns=quest3d::qpc_nanoseconds();
    h.source.kind=quest3d::source_kind::monitor; h.source.native_handle=11; h.source.selection_id=9007199254740993ULL;
    h.source.parent_left=-200; h.source.parent_top=0; h.source.parent_width=200; h.source.parent_height=200;
    check_v3(sizeof(h)==192&&quest3d::valid_header(p)&&quest3d::valid_source_identity(p,h.source),"valid v3 syntax");
    source_publisher writer; writer.put(h);
    quest3d::frame_reader reader(writer.names); quest3d::frame output;
    check_v3(reader.read(output,0)==quest3d::read_result::fresh,"read complete v3 frame");
    check_v3(output.source==h.source&&output.pixels.size()==8192&&output.pixels.front()==0x79&&output.pixels.back()==0x79,"source and offset192 pixel ownership");
    auto wrong_names=writer.names; wrong_names.protocol=2;
    quest3d::frame_reader wrong(wrong_names);
    check_v3(wrong.read(output,0)==quest3d::read_result::invalid,"v2 never auto-upgrades mapping bytes");
    auto malformed=h; malformed.prefix.frame_id=2; malformed.source.reserved[15]=1; writer.put(malformed);
    check_v3(reader.read(output,0)==quest3d::read_result::invalid,"reject extension reserved bytes");
    malformed=h; malformed.prefix.frame_id=2; malformed.source.selection_id=0; writer.put(malformed);
    check_v3(reader.read(output,0)==quest3d::read_result::invalid,"reject absent selection lifetime");
    malformed=h; malformed.prefix.frame_id=2; malformed.source.parent_width=199; writer.put(malformed);
    check_v3(reader.read(output,0)==quest3d::read_result::invalid,"reject crop outside immutable parent");
    quest3d::capture_registry registry;
    auto first=registry.publish(p,h.source); check_v3(static_cast<bool>(first),"publish source metadata");
    auto switched=h.source; ++switched.selection_id;
    auto second=registry.publish(p,switched); check_v3(second->geometry_revision>first->geometry_revision,"selection-only change invalidates old metadata");
    check_v3(first->source==h.source,"old immutable source preserved");
    auto again=registry.publish(p,h.source); check_v3(again->geometry_revision>second->geometry_revision,"same-geometry source roundtrip invalidates");
    check_v3(!registry.publish(p),"v3 prefix alone cannot become authoritative");
    for(auto kind:{quest3d::source_kind::video,quest3d::source_kind::photo}) {
      auto file=h; file.source.kind=kind; file.source.native_handle=0; file.source.parent_left=0; file.prefix.source_left=0;
      check_v3(quest3d::valid_source_identity(file.prefix,file.source),"valid display-only file source");
      file.prefix.flags|=8; check_v3(!quest3d::valid_source_identity(file.prefix,file.source),"files cannot enable OS input");
    }
    auto win=h; win.source.kind=quest3d::source_kind::window;
    check_v3(!quest3d::valid_source_identity(win.prefix,win.source),"window requires process lifetime");
    win.source.process_id=5; win.source.creation_filetime=1234;
    check_v3(quest3d::valid_source_identity(win.prefix,win.source),"window lifetime syntax");
    ++p.frame_id; writer.put(h); check_v3(reader.read(output,0)==quest3d::read_result::fresh,"recover after malformed data");
    writer.die(); check_v3(reader.read(output,0)==quest3d::read_result::invalid,"dead publisher invalidates before stale image reuse");
    std::cout<<"PASS: v3 offsets192, explicit version, isolated mapping, source lifetime, parent bounds, file gate, immutable revisions, owner death\n";
    return 0;
  } catch(const std::exception &error) { std::cerr<<error.what()<<'\n'; return 1; }
}
