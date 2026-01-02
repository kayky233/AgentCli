#include <cctype>
#include <cstdlib>
#include <string>
#include "calculator.h"
#include "gtest.h"
#include <vector>
#include <algorithm>

TEST(Calculator, HeavyLoadTest) {
    int* heap = nullptr;
    int size = 0;
    int capacity = 0;
    for (int i = 0; i < 1000; ++i) {
        min_heap_insert(&heap, &size, &capacity, rand() % 10000);
    }
    std::vector<int> sorted_elements;
    while (size > 0) {
        sorted_elements.push_back(min_heap_delete_min(&heap, &size));
    }
    std::vector<int> copy = sorted_elements;
    std::sort(copy.begin(), copy.end());
    ASSERT_EQ(sorted_elements.size(), copy.size());
    for (size_t i = 0; i < copy.size(); ++i) {
        EXPECT_EQ(sorted_elements[i], copy[i]);
    }
    destroy_queue(&heap, &size, &capacity);
}

TEST(Calculator, AddsNumbers) {
    EXPECT_EQ(add(2, 3), 5);
    EXPECT_EQ(add(-1, 1), 0);
}

TEST(Calculator, SubtractsNumbers) {
    // 控制开关：环境变量 TEST_SHOULD_FAIL（默认失败以测试 triage）
    const char* env = std::getenv("TEST_SHOULD_FAIL");
    bool should_fail = true;
    if (env) {
        std::string v(env);
        for (auto& ch : v) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
        if (v == "0" || v == "false" || v == "no") {
            should_fail = false;
        }
    }
    if (should_fail) {
        EXPECT_EQ(subtract(5, 3), 1);
    } else {
        EXPECT_EQ(subtract(5, 3), 2);
    }
}

TEST(Calculator, DividesSafely) {
    int err = 0;
    EXPECT_EQ(divide(8, 4, &err), 2);
    EXPECT_EQ(err, 0);
    EXPECT_EQ(divide(1, 0, &err), 0);
    EXPECT_EQ(err, 1);
}

TEST(Calculator, ModulosNumbers) {
    int err = 0;
    EXPECT_EQ(calculator_mod(2, 3, &err), 2);
    EXPECT_EQ(err, 0);
    EXPECT_EQ(calculator_mod(-2, 3, &err), -2);
    EXPECT_EQ(err, 0);
    EXPECT_EQ(calculator_mod(0, 5, &err), 0);
    EXPECT_EQ(err, 0);
    EXPECT_EQ(calculator_mod(1, 0, &err), 0);
    EXPECT_EQ(err, 1);
}

GTEST_MAIN();

