#include "calculator.h"
#include "gtest.h"

TEST(Calculator, AddsNumbers) {
    EXPECT_EQ(add(2, 3), 5);
    EXPECT_EQ(add(-1, 1), 0);
}

TEST(Calculator, SubtractsNumbers) {
    // This one is intentionally failing to exercise TestTriage parsing.
    EXPECT_EQ(subtract(5, 3), 1);
}

TEST(Calculator, DividesSafely) {
    int err = 0;
    EXPECT_EQ(divide(8, 4, &err), 2);
    EXPECT_EQ(err, 0);
    EXPECT_EQ(divide(1, 0, &err), 0);
    EXPECT_EQ(err, 1);
}

GTEST_MAIN();

