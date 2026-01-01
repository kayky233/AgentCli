#include "gtest.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <sstream>

namespace fs = std::filesystem;

namespace testing {

namespace {
std::vector<TestInfo>& Registry() {
    static std::vector<TestInfo> registry;
    return registry;
}

struct TestContext {
    std::vector<AssertionRecord> failures;
};

thread_local TestContext* current_context = nullptr;
std::string g_output_path;
}  // namespace

TestRegistrar::TestRegistrar(const std::string& suite, const std::string& name, std::function<void()> func) {
    Registry().push_back(TestInfo{suite, name, std::move(func)});
}

void AddFailure(const std::string& file, int line, const std::string& message, bool fatal) {
    if (!current_context) {
        return;
    }
    current_context->failures.push_back(AssertionRecord{fatal, message, file, line});
    if (fatal) {
        throw AssertionException();
    }
}

void InitGoogleTest(int* argc, char** argv) {
    for (int i = 1; i < *argc; ++i) {
        std::string arg(argv[i]);
        std::string prefix = "--gtest_output=xml:";
        if (arg.rfind(prefix, 0) == 0) {
            g_output_path = arg.substr(prefix.size());
        }
    }
}

static void WriteXmlReport(const std::vector<TestInfo>& tests,
                           const std::vector<std::vector<AssertionRecord>>& failures,
                           const std::vector<double>& durations_ms) {
    if (g_output_path.empty()) return;

    std::map<std::string, std::vector<size_t>> suite_map;
    for (size_t i = 0; i < tests.size(); ++i) {
        suite_map[tests[i].suite].push_back(i);
    }

    fs::create_directories(fs::path(g_output_path).parent_path());
    std::ofstream out(g_output_path);
    if (!out.is_open()) {
        std::cerr << "Warning: failed to write XML report to " << g_output_path << std::endl;
        return;
    }
    int total_failures = 0;
    for (const auto& f : failures) total_failures += static_cast<int>(f.size());

    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out << "<testsuites tests=\"" << tests.size() << "\" failures=\"" << total_failures
        << "\" disabled=\"0\" errors=\"0\">\n";
    for (const auto& [suite, indices] : suite_map) {
        int suite_failures = 0;
        for (size_t idx : indices) suite_failures += static_cast<int>(failures[idx].size());
        double suite_time = 0.0;
        for (size_t idx : indices) suite_time += durations_ms[idx] / 1000.0;
        out << "  <testsuite name=\"" << suite << "\" tests=\"" << indices.size()
            << "\" failures=\"" << suite_failures << "\" disabled=\"0\" errors=\"0\" time=\""
            << suite_time << "\">\n";
        for (size_t idx : indices) {
            const auto& t = tests[idx];
            out << "    <testcase name=\"" << t.name << "\" status=\"run\" result=\""
                << (failures[idx].empty() ? "completed" : "failed") << "\" time=\""
                << std::fixed << std::setprecision(3) << durations_ms[idx] / 1000.0
                << "\" classname=\"" << suite << "\">";
            if (!failures[idx].empty()) {
                out << "\n";
                for (const auto& fail : failures[idx]) {
                    out << "      <failure message=\"" << fail.message << "\" type=\"\">\n";
                    out << fail.file << ":" << fail.line << "\n";
                    out << fail.message << "\n";
                    out << "      </failure>\n";
                }
                out << "    </testcase>\n";
            } else {
                out << "</testcase>\n";
            }
        }
        out << "  </testsuite>\n";
    }
    out << "</testsuites>\n";
}

int RunAllTests() {
    const auto& tests = Registry();
    std::cout << "[==========] Running " << tests.size() << " tests from "
              << tests.size() << " test suites." << std::endl;
    std::cout << "[----------] Global test environment set-up." << std::endl;

    int failed = 0;
    std::vector<std::vector<AssertionRecord>> failures(tests.size());
    std::vector<double> durations_ms(tests.size(), 0.0);
    for (size_t i = 0; i < tests.size(); ++i) {
        const auto& test = tests[i];
        std::cout << "[ RUN      ] " << test.suite << "." << test.name << std::endl;
        TestContext ctx;
        current_context = &ctx;
        auto start = std::chrono::steady_clock::now();
        try {
            test.func();
        } catch (const AssertionException&) {
            // fatal assertion handled
        } catch (const std::exception& ex) {
            AddFailure("unknown", 0, std::string("Unhandled exception: ") + ex.what(), true);
        } catch (...) {
            AddFailure("unknown", 0, "Unhandled non-standard exception", true);
        }
        auto end = std::chrono::steady_clock::now();
        double ms = std::chrono::duration_cast<std::chrono::milliseconds>(end - start).count();
        durations_ms[i] = ms;
        failures[i] = ctx.failures;
        current_context = nullptr;

        if (ctx.failures.empty()) {
            std::cout << "[       OK ] " << test.suite << "." << test.name << " (" << ms << " ms)" << std::endl;
        } else {
            ++failed;
            std::cout << "[  FAILED  ] " << test.suite << "." << test.name << " (" << ms << " ms)" << std::endl;
            for (const auto& f : ctx.failures) {
                std::cout << f.file << ":" << f.line << ": " << f.message << std::endl;
            }
        }
    }

    std::cout << "[----------] Global test environment tear-down" << std::endl;
    std::cout << "[==========] " << tests.size() << " tests ran." << std::endl;
    std::cout << "[  PASSED  ] " << (tests.size() - failed) << " tests." << std::endl;
    if (failed > 0) {
        std::cout << "[  FAILED  ] " << failed << " tests, listed below:" << std::endl;
        for (size_t i = 0; i < tests.size(); ++i) {
            if (!failures[i].empty()) {
                std::cout << "[  FAILED  ] " << tests[i].suite << "." << tests[i].name << std::endl;
            }
        }
    }

    WriteXmlReport(tests, failures, durations_ms);
    return failed == 0 ? 0 : 1;
}

}  // namespace testing

