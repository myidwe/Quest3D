"""새 격리 후보에 역사 audio packet API 호환 patch를 명시적으로 적용한다.

기존 바이너리의 원 대응 소스를 복원했다고 주장하지 않는다. 실행 중 제품,
역사 소스와 dependency 입력을 수정하지 않으며 호스트 서버를 실행하지 않는다.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
from pathlib import Path
import shutil

_spec = importlib.util.spec_from_file_location(
    "compat_source_preparation", Path(__file__).with_name("prepare_host_source.py"))
preparation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preparation)

OLD = b"      TUPLE_2D_REF(channel_data, packet_data, *packet);\n"
NEW = (
    b"      auto &channel_data = packet->first;  ///< Session channel carried by the audio packet.\n"
    b"      auto &packet_data = packet->second;  ///< Encoded Opus payload carried by the audio packet.\n"
)
TEST_PATH = "tests/unit/test_quest3d_audio_packet_compat.cpp"
TEST_SOURCE = '''/**
 * @file tests/unit/test_quest3d_audio_packet_compat.cpp
 * @brief Check normal/file packet channel, payload and scope through the real queue API.
 */
#include "src/audio.h"
#include "tests/tests_common.h"

/** @brief Normal capture retains its channel and complete payload without a file gate. */
TEST(QuestAudioPacketCompat, NormalCaptureQueuePreservesChannelAndPayload) {
  int channel = 7;
  audio::buffer_t encoded {8};
  for (unsigned i = 0; i < encoded.size(); ++i) {
    encoded[i] = static_cast<std::uint8_t>(i * 17);
  }
  const auto payload_address = encoded.begin();
  safe::queue_t<audio::packet_t> packets {2};
  packets.raise(&channel, std::move(encoded));
  auto packet = packets.pop(std::chrono::milliseconds(10));
  ASSERT_TRUE(packet);
  EXPECT_EQ(packet->first, &channel);
  EXPECT_EQ(packet->second.begin(), payload_address);
  ASSERT_EQ(packet->second.size(), 8U);
  for (unsigned i = 0; i < packet->second.size(); ++i) {
    EXPECT_EQ(packet->second[i], static_cast<std::uint8_t>(i * 17));
  }
  EXPECT_FALSE(packet->file_scope);
}

/** @brief File packets preserve the same gate identity through enqueue/dequeue moves. */
TEST(QuestAudioPacketCompat, FileCaptureQueuePreservesScopeIdentityAndInvalidation) {
  int channel = 9;
  auto scope = std::make_shared<quest3d::pcm::packet_scope>();
  safe::queue_t<audio::packet_t> packets {2};
  packets.raise(&channel, audio::buffer_t {4, std::uint8_t {0x5a}}, scope);
  auto packet = packets.pop(std::chrono::milliseconds(10));
  ASSERT_TRUE(packet);
  EXPECT_EQ(packet->first, &channel);
  ASSERT_EQ(packet->second.size(), 4U);
  EXPECT_EQ(packet->second[3], 0x5a);
  ASSERT_EQ(packet->file_scope, scope);
  scope->stop();
  EXPECT_FALSE(packet->file_scope->active);
}
'''


def compatibility_patch(source: bytes) -> bytes:
    """한 곳의 tuple 접근만 변경하고 예상 API/site 불일치 시 거부한다."""
    crlf_old = OLD.replace(b"\n", b"\r\n")
    if source.count(OLD) + source.count(crlf_old) != 1:
        raise ValueError("Expected exactly one historical tuple access site")
    newline = b"\r\n" if source.count(crlf_old) else b"\n"
    return source.replace(OLD.replace(b"\n", newline), NEW.replace(b"\n", newline), 1)


def prepare(historical: Path, output: Path, finalize_only=False) -> dict:
    historical = historical.resolve()
    output = output.resolve()
    if output.is_relative_to(historical) or historical.is_relative_to(output):
        raise FileExistsError("Use an independent new candidate directory")
    if finalize_only:
        if not output.is_dir() or {p.name for p in output.iterdir()} != {"source"}:
            raise FileExistsError("Finalize only an unpatched source-only candidate directory")
    elif output.exists():
        raise FileExistsError("Use an independent new candidate directory")
    original = json.loads((historical / "source-preparation.json").read_text("utf-8"))
    if original["upstream_commit"] != preparation.PIN:
        raise ValueError("Historical upstream identity differs")
    archives = historical / "archives"
    records = {"sunshine-upstream.tar": original["upstream_archive_sha256"]}
    records.update({r["archive"]: r["sha256"] for r in original["submodules"] if r["included"]})
    if {p.name for p in archives.glob("*.tar")} != set(records):
        raise ValueError("Historical archive inventory differs")
    for name, expected in records.items():
        preparation.bundle.assert_regular(archives / name, historical)
        if preparation.bundle.digest(archives / name) != expected:
            raise ValueError("Historical source archive identity differs")
    overlay = historical / "historical-overlay"
    if preparation.hashes(overlay) != original["overlay_files"]:
        raise ValueError("Historical overlay identity differs")
    deps = historical / "dependencies"
    dependency_records = original["dependencies"]
    for name, record in dependency_records.items():
        preparation.bundle.assert_regular(deps / name, historical)
        if preparation.bundle.digest(deps / name) != record["sha256"]:
            raise ValueError("Shared dependency archive identity differs")
    source = output / "source"
    if not finalize_only:
        output.mkdir(parents=True)
        for name in sorted(records):
            prefix = "" if name == "sunshine-upstream.tar" else Path(name[:-4].replace("--", "/"))
            preparation.extract_archive((archives / name).read_bytes(), source / prefix)
        for name, expected in original["overlay_files"].items():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(overlay / name, target)
            if preparation.bundle.digest(target) != expected:
                raise ValueError("Candidate overlay copy differs")
    if preparation.hashes(source) != original["source_files"]:
        raise ValueError("Unpatched candidate differs from historical archive authority")
    stream = source / "src/stream.cpp"
    before = stream.read_bytes()
    after = compatibility_patch(before)
    stream.write_bytes(after)
    test = source / TEST_PATH
    if test.exists():
        raise FileExistsError("Candidate test path already exists in historical source")
    test.write_text(TEST_SOURCE, "utf-8", newline="\n")
    patch = "".join(difflib.unified_diff(
        before.decode().splitlines(keepends=True), after.decode().splitlines(keepends=True),
        fromfile="a/src/stream.cpp", tofile="b/src/stream.cpp"))
    patch += "".join(difflib.unified_diff(
        [], TEST_SOURCE.splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + TEST_PATH))
    (output / "audio-packet-api-compat.patch").write_text(patch, "utf-8", newline="\n")
    shutil.copyfile(Path(__file__).with_name("rebuild_host_compat_candidate.sh"),
                    output / "rebuild_host_compat_candidate.sh")
    shutil.copyfile(historical / "toolchain.lock.json", output / "toolchain.lock.json")
    state = {
        "schema": 1, "kind": "historical-api-compatibility-candidate", "published": False,
        "upstream_commit": preparation.PIN, "historical_binary_sha256": preparation.HOST_SHA,
        "historical_source_manifest_sha256": preparation.bundle.digest(historical / "source-preparation.json"),
        "base_source_file_count": len(original["source_files"]), "base_archive_identity": records,
        "candidate_version": "2026.930.1", "candidate_branch": "quest3d-api-compat-candidate",
        "candidate_commit_label": preparation.PIN + "-api-compat-candidate",
        "shared_dependencies": "../" + historical.name + "/dependencies",
        "shared_dependency_archives": dependency_records,
        "patch_sha256": preparation.bundle.digest(output / "audio-packet-api-compat.patch"),
        "modified_files": {"src/stream.cpp": {"before_sha256": preparation.sha(before),
                                               "after_sha256": preparation.sha(after)}},
        "added_files": {TEST_PATH: preparation.bundle.digest(test)},
        "historical_binary_source_correspondence_verified": False, "release_source_gate": False,
        "runtime_replaced": False, "server_started": False, "hardware_validation": False,
        "build_success": False,
        "notes": ["역사 바이너리의 누락 원본 복원 아님 · 공개 release 대응 소스 미검증",
                  "제품 변경 한 곳과 queue API 테스트만 명시적 patch 적용 · 추가 누락 시 중단",
                  "공유 dependency는 읽기 입력이며 source/build/temp/npm 출력은 후보 폴더에 격리"],
    }
    (output / "candidate-provenance.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"prepared": True, "base_source_files": len(original["source_files"]),
            "modified_product_files": 1, "new_test_files": 1, "release_source_gate": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--finalize-only", action="store_true")
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.historical, args.output, args.finalize_only), indent=2))


if __name__ == "__main__":
    main()
