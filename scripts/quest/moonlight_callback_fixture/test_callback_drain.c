// Execute the actual patched Connection.c and native callback dispatcher.
// The stream stage is idle; no server, socket stream, Quest, audio or XR fixture
// claims are made. Other Moonlight functions link from the real pinned library.
#include <pthread.h>
#include <stdatomic.h>
#include <assert.h>
#include <errno.h>
#include <sched.h>
#include <stdio.h>
#include <time.h>

static int inject_create_failure;
static int inject_join_failure;
static int fixture_create(pthread_t* t, const pthread_attr_t* a, void* (*fn)(void*), void* arg) {
    return inject_create_failure ? EAGAIN : pthread_create(t, a, fn, arg);
}
static int fixture_join(pthread_t t, void** out) {
    return inject_join_failure ? EINVAL : pthread_join(t, out);
}
#define pthread_create fixture_create
#define pthread_join fixture_join
#include "Connection.c"
#undef pthread_create
#undef pthread_join

static pthread_mutex_t test_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t test_condition = PTHREAD_COND_INITIALIZER;
static bool entered;
static bool release_callback;
static bool stop_in_callback;
static bool check_self;
static int callback_error;
static atomic_int callback_count;
static atomic_int drain_result;
static pthread_barrier_t producer_barrier;

static void callback(int error) {
    if (stop_in_callback) LiStopConnection();
    if (check_self) assert(LiWaitForConnectionCallbacks() == CL_DRAIN_SELF);
    pthread_mutex_lock(&test_mutex);
    callback_error = error;
    entered = true;
    atomic_fetch_add(&callback_count, 1);
    pthread_cond_broadcast(&test_condition);
    while (!release_callback) pthread_cond_wait(&test_condition, &test_mutex);
    pthread_mutex_unlock(&test_mutex);
}

static void wait_for_entry(void) {
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_sec += 5;
    pthread_mutex_lock(&test_mutex);
    while (!entered) assert(pthread_cond_timedwait(&test_condition, &test_mutex, &deadline) == 0);
    pthread_mutex_unlock(&test_mutex);
}

static void release_task(void) {
    pthread_mutex_lock(&test_mutex);
    release_callback = true;
    pthread_cond_broadcast(&test_condition);
    pthread_mutex_unlock(&test_mutex);
}

static void arm(void) {
    assert(ClDrainCanBegin() == 0);
    entered = false;
    release_callback = false;
    callback_error = 0;
    stop_in_callback = false;
    check_self = false;
    ConnectionInterrupted = false;
    ClDrainEnable(callback);
}

static void* wait_drain(void* unused) {
    (void)unused;
    atomic_store(&drain_result, LiWaitForConnectionCallbacks());
    return NULL;
}

static void* simultaneous_issue(void* index) {
    pthread_barrier_wait(&producer_barrier);
    ClInternalConnectionTerminated(3000 + (int)(intptr_t)index);
    return NULL;
}

static void wait_for_join_begin(void) {
    struct timespec deadline, now;
    clock_gettime(CLOCK_MONOTONIC, &deadline);
    deadline.tv_sec += 5;
    for (;;) {
        ClDrainLock();
        bool joining = callbackDrain.joining;
        ClDrainUnlock();
        if (joining) return;
        clock_gettime(CLOCK_MONOTONIC, &now);
        assert(now.tv_sec < deadline.tv_sec || (now.tv_sec == deadline.tv_sec && now.tv_nsec < deadline.tv_nsec));
        sched_yield();
    }
}

int main(int argc, char** argv) {
    (void)argv;
    // Failures run in separate processes: production failures are sticky.
    if (argc == 2) {
        arm();
        inject_create_failure = 1;
        assert(ClDrainIssue(91) == CL_DRAIN_CREATE_FAILED);
        LiStopConnection();
        assert(LiWaitForConnectionCallbacks() == CL_DRAIN_CREATE_FAILED);
        assert(ClDrainCanBegin() == CL_DRAIN_CREATE_FAILED);
        assert(atomic_load(&callback_count) == 0);
        puts("create failure retained: PASS");
        return 0;
    }
    if (argc == 3) {
        arm();
        assert(ClDrainIssue(92) == 0);
        wait_for_entry();
        release_task();
        LiStopConnection();
        inject_join_failure = 1;
        assert(LiWaitForConnectionCallbacks() == CL_DRAIN_JOIN_FAILED);
        assert(ClDrainCanBegin() == CL_DRAIN_JOIN_FAILED);
        inject_join_failure = 0;
        assert(LiWaitForConnectionCallbacks() == CL_DRAIN_JOIN_FAILED);
        // Fixture-only join prevents a test resource leak. It does not change
        // the failed production result or let the production owner restart.
        assert(pthread_join(callbackDrain.thread, NULL) == 0);
        puts("join failure retained: PASS");
        return 0;
    }
    assert(LiWaitForConnectionCallbacks() == 0); // no task
    for (int i = 0; i < 100; ++i) {
        arm();
        check_self = true;
        stop_in_callback = true;
        ClInternalConnectionTerminated(1000 + i);
        wait_for_entry();
        assert(callback_error == 1000 + i);
        // An outstanding task must reject before dereferencing new arguments.
        assert(LiStartConnection(NULL, NULL, NULL, NULL, NULL, NULL, 0, NULL, 0) == CL_DRAIN_BUSY);
        ClInternalConnectionTerminated(2000 + i);
        pthread_t waiter;
        atomic_store(&drain_result, 12345);
        assert(pthread_create(&waiter, NULL, wait_drain, NULL) == 0);
        wait_for_join_begin();
        assert(atomic_load(&drain_result) == 12345);
        assert(LiWaitForConnectionCallbacks() == CL_DRAIN_BUSY);
        release_task();
        assert(pthread_join(waiter, NULL) == 0);
        assert(atomic_load(&drain_result) == 0);
        assert(LiWaitForConnectionCallbacks() == 0); // idempotent
        assert(atomic_load(&callback_count) == i + 1);
        assert(ClDrainCanBegin() == 0);
    }
    arm();
    assert(LiWaitForConnectionCallbacks() == CL_DRAIN_BUSY);
    LiInterruptConnection();
    ClInternalConnectionTerminated(99);
    LiStopConnection();
    assert(LiWaitForConnectionCallbacks() == 0);
    assert(atomic_load(&callback_count) == 100);
    // Simultaneous audio/video/control failure reports produce one task and
    // keep its original error. The selected producer is intentionally unordered.
    arm();
    pthread_t producers[16];
    assert(pthread_barrier_init(&producer_barrier, NULL, 16) == 0);
    for (int i = 0; i < 16; ++i) assert(pthread_create(&producers[i], NULL, simultaneous_issue, (void*)(intptr_t)i) == 0);
    for (int i = 0; i < 16; ++i) assert(pthread_join(producers[i], NULL) == 0);
    wait_for_entry();
    assert(callback_error >= 3000 && callback_error < 3016);
    LiStopConnection();
    release_task();
    assert(LiWaitForConnectionCallbacks() == 0);
    assert(atomic_load(&callback_count) == 101);
    assert(pthread_barrier_destroy(&producer_barrier) == 0);
    stage = STAGE_PLATFORM_INIT;
    assert(LiWaitForConnectionCallbacks() == CL_DRAIN_BUSY);
    stage = STAGE_NONE; // explicit stage fixture, no platform was initialized
    puts("actual Connection.c: 100 held callback/restart cycles, stop from callback, self/concurrent drain, interruption, 16 simultaneous error producers: PASS");
}
