// Runs only the production kernel mutex guard: no capture, network or OS input.
#include "quest3d_instance.h"
#include <iostream>
#include <string>

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  const auto port = std::stoul(argv[2]);
  if (port == 0 || port > 65535) return 2;
  quest3d::instance_guard guard;
  if (!guard.acquire(static_cast<std::uint16_t>(port))) return 9;
  if (std::string(argv[1]) == "--hold") {
    std::cout << "READY" << std::endl;
    std::cin.get();
    return 0;
  }
  return std::string(argv[1]) == "--probe" ? 0 : 2;
}
