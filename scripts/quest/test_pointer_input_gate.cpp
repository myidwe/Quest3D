#include "pc_pointer_input_gate.h"
#include <cassert>
#include <iostream>

int main() {
    nightfall::PcPointerInputGate gate;
    assert(gate.permits(4, true) && !gate.permits(4, false));
    assert(!gate.permits(-1, true));
    gate.set(4, true);
    assert(gate.permits(4, false, "epoch1") && !gate.permits(5, false, "epoch1"));
    gate.set(4, false);
    assert(!gate.permits(4, false) && gate.permits(4, true));
    gate.set(4, true);
    gate.set(5, true);
    assert(!gate.permits(4, false, "epoch1") && gate.permits(5, false, "epoch1"));
    gate.mark_uncertain(5, "epoch1");
    gate.set(5, true);
    assert(!gate.permits(5, false, "epoch1") && gate.permits(5, true, "epoch1"));
    assert(gate.permits(5, false, "epoch2"));
    gate.set(4, true);
    gate.set(5, true);
    assert(!gate.permits(5, false, "epoch1"));
    gate.set(-1, true);
    assert(!gate.permits(5, false));
    std::cout << "Pointer native opt-in: default OFF, exact host, revoke, switch PASS\n";
}
