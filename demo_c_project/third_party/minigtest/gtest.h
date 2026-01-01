#pragma once

#include <exception>
#include <functional>
#include <sstream>
#include <string>
#include <vector>

namespace testing {

struct AssertionRecord {
    bool fatal;
    std::string message;
    std::string file;
    int line;
};

struct TestInfo {
    std::string suite;
    std::string name;
    std::function<void()> func;
};

class AssertionException : public std::exception {
public:
    const char* what() const noexcept override { return "fatal assertion"; }
};

class TestRegistrar {
public:
    TestRegistrar(const std::string& suite, const std::string& name, std::function<void()> func);
};

void InitGoogleTest(int* argc, char** argv);
int RunAllTests();

void AddFailure(const std::string& file, int line, const std::string& message, bool fatal);

template <typename A, typename B>
void ExpectEqual(const A& a, const B& b, const char* a_expr, const char* b_expr, const char* file, int line, bool fatal) {
    if (a == b) return;
    std::ostringstream oss;
    oss << "Expected equality of these values:\n  " << a_expr << "\n    Which is: " << a << "\n  " << b_expr << "\n    Which is: " << b;
    AddFailure(file, line, oss.str(), fatal);
}

template <typename A, typename B>
void ExpectNotEqual(const A& a, const B& b, const char* a_expr, const char* b_expr, const char* file, int line, bool fatal) {
    if (a != b) return;
    std::ostringstream oss;
    oss << "Expected inequality of these values:\n  " << a_expr << "\n    Which is: " << a << "\n  " << b_expr << "\n    Which is: " << b;
    AddFailure(file, line, oss.str(), fatal);
}

inline void ExpectTrue(bool cond, const char* expr, const char* file, int line, bool fatal) {
    if (cond) return;
    std::ostringstream oss;
    oss << "Expected: " << expr << " is true";
    AddFailure(file, line, oss.str(), fatal);
}

}  // namespace testing

#define TEST(Suite, Name) \
    void Suite##_##Name##_Test(); \
    static ::testing::TestRegistrar registrar_##Suite##_##Name(#Suite, #Name, &Suite##_##Name##_Test); \
    void Suite##_##Name##_Test()

#define EXPECT_EQ(a, b) ::testing::ExpectEqual((a), (b), #a, #b, __FILE__, __LINE__, false)
#define ASSERT_EQ(a, b) ::testing::ExpectEqual((a), (b), #a, #b, __FILE__, __LINE__, true)
#define EXPECT_NE(a, b) ::testing::ExpectNotEqual((a), (b), #a, #b, __FILE__, __LINE__, false)
#define ASSERT_NE(a, b) ::testing::ExpectNotEqual((a), (b), #a, #b, __FILE__, __LINE__, true)
#define EXPECT_TRUE(expr) ::testing::ExpectTrue((expr), #expr, __FILE__, __LINE__, false)
#define ASSERT_TRUE(expr) ::testing::ExpectTrue((expr), #expr, __FILE__, __LINE__, true)
#define EXPECT_FALSE(expr) ::testing::ExpectTrue(!(expr), #expr, __FILE__, __LINE__, false)
#define ASSERT_FALSE(expr) ::testing::ExpectTrue(!(expr), #expr, __FILE__, __LINE__, true)

#define GTEST_MAIN() \
    int main(int argc, char** argv) { \
        ::testing::InitGoogleTest(&argc, argv); \
        return ::testing::RunAllTests(); \
    }

