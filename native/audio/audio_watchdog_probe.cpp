// Exercises only READY/lifetime handles. Never calls an audio or network API.
#include "audio_watchdog.h"
#include <iostream>
#include <string>

int main(int argc, char **argv) {
  const auto wait_ms =
      argc == 2 ? static_cast<DWORD>(std::stoul(argv[1])) : 2000;
  platf::audio::watchdog_gate_t gate;
  std::cout << "STARTED" << std::endl;
  if (!gate.wait_ready(wait_ms)) {
    std::cout << "REFUSED" << std::endl;
    return 3;
  }
  std::cout << "READY" << std::endl;
  std::string command;
  while (std::getline(std::cin, command)) {
    if (command == "health")
      std::cout << (gate.alive() ? "ALIVE" : "DEAD") << std::endl;
    else
      break;
  }
  return 0;
}
