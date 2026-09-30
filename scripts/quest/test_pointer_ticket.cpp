#include "pointer_ticket.h"
#include <cassert>
#include <iostream>
int main() {
    using namespace quest3d;
    PointerTicket ticket;
    SubmissionToken clicked{std::string(32, 'a'), 9, 4294967295U, 99};
    auto later = clicked; later.frame = 1; later.lease = 100;
    auto issue = [&] { return ticket.issue(clicked, 2, 3, 4, 1000000, 800000); };
    auto valid = [&](uint64_t id, int64_t now = 1100000) { return ticket.current(id, now, 2, 3, 4, later, true); };
    auto id = issue(); assert(valid(id));
    ticket.begin(true, false); ticket.finish(true); assert(valid(id)); // newer frame is allowed, clicked token retained
    assert(!ticket.current(id, 1100000, 2, 3, 4, later, false)); // cycle currently in progress
    assert(!valid(id, 1250001)); assert(valid(id, 1250000)); // original submission TTL, never refreshed
    assert(!valid(id, 999999));
    assert(!ticket.current(id, 1100000, 3, 3, 4, later, true));
    assert(!ticket.current(id, 1100000, 2, 4, 4, later, true));
    assert(!ticket.current(id, 1100000, 2, 3, 5, later, true));
    auto restarted = later; ++restarted.decoder;
    assert(!ticket.current(id, 1100000, 2, 3, 4, restarted, true));
    restarted = later; restarted.transport[0] = 'b';
    assert(!ticket.current(id, 1100000, 2, 3, 4, restarted, true));
    ticket.finish(false); ticket.finish(true); assert(!valid(id)); // success cannot resurrect failure
    id = issue(); ticket.begin(false, false); ticket.finish(true); assert(!valid(id));
    id = issue(); ticket.begin(true, true); ticket.finish(true); assert(!valid(id)); // missing xrEndFrame
    id = issue(); auto next = issue(); assert(next != id && !valid(id) && valid(next));
    ticket.invalidate(); assert(!valid(next));
    id = ticket.issue(clicked, 2, 3, 4, 1000000, 500000); assert(!valid(id)); // pixel TTL independent
    assert(!ticket.issue({}, 2, 3, 4, 1000000, 800000));
    std::cout << "pointer ticket lifecycle/TTL/new-frame/restart/skip regressions passed\n";
}
