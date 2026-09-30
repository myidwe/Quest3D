// Local publisher integration probe. No network listener or OS input injection.
#include "quest3d_control.h"
#include "quest3d_frame.h"
#include <Windows.h>
#include <iomanip>
#include <iostream>
#include <random>
#include <sstream>

int main(int argc, char **argv) {
  if (argc < 2 || argc > 4) {
    std::cerr << "usage: control_bridge_probe DIRECTORY [2d|3d] [DISPARITY]\n";
    return 2;
  }
  quest3d::control_mailbox mailbox {std::filesystem::absolute(argv[1])};
  auto result = mailbox.get(quest3d::qpc_nanoseconds());
  if (result.code != 200 || argc == 2) {
    std::cout << result.body.dump() << '\n';
    return result.code == 200 ? 0 : 2;
  }
  std::random_device random;
  std::ostringstream id;
  for (int i = 0; i < 4; ++i) { id << std::hex << std::setfill('0') << std::setw(8) << static_cast<std::uint32_t>(random()); }
  const double disparity = argc == 4 ? std::stod(argv[3]) : result.body.at("disparity").get<double>();
  nlohmann::json command {{"version", 1}, {"session_id", result.body["session_id"]}, {"request_id", id.str()},
                          {"seq", 1}, {"expected_revision", result.body["revision"]}, {"mode", argv[2]}, {"disparity", disparity}};
  result = mailbox.post(command.dump(), quest3d::qpc_nanoseconds());
  std::cout << result.body.dump() << '\n';
  if (result.code != 202) { return 3; }
  const auto deadline = GetTickCount64() + 6000;
  while (GetTickCount64() < deadline) {
    result = mailbox.get(quest3d::qpc_nanoseconds());
    if (result.code != 200) { std::cout << result.body.dump() << '\n'; return 4; }
    const auto &outcome = result.body.at("last_request").at("outcome");
    if (outcome != "pending") {
      std::cout << result.body.dump() << '\n';
      return outcome == "applied" ? 0 : 5;
    }
    Sleep(20);
  }
  std::cerr << "No confirmed publisher acknowledgement within deadline\n";
  return 6;
}
