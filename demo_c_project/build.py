import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

BUILD_DIR = Path("build")
OBJ_DIR = BUILD_DIR / "obj"
BIN_DIR = BUILD_DIR / "bin"
TEST_DIR = BUILD_DIR / "tests"


def find_compilers():
    system_compilers = []
    if os.name == "nt":
        system_compilers.extend(["cl", "gcc", "clang"])
    else:
        system_compilers.extend(["gcc", "clang", "cc"])
    for c in system_compilers:
        if shutil.which(c):
            return c
    return None


def ensure_dirs():
    for d in [OBJ_DIR, BIN_DIR, TEST_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def run(cmd: List[str], env=None) -> int:
    print(">>>", " ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    sys.stdout.write(proc.stdout)
    return proc.returncode


def build_with_msvc():
    ensure_dirs()
    cflags = ["/nologo", "/MD", "/Iinclude"]
    cppflags = cflags + ["/std:c++17", "/EHsc", "/Ithird_party/minigtest"]
    objs = [
        (Path("src/calculator.c"), OBJ_DIR / "calculator.obj"),
        (Path("third_party/minigtest/gtest.cpp"), OBJ_DIR / "gtest.obj"),
        (Path("tests/test_calculator.cpp"), OBJ_DIR / "test_calculator.obj"),
        (Path("src/main.c"), OBJ_DIR / "main.obj"),
    ]
    for src, obj in objs:
        if src.suffix == ".c":
            rc = run(["cl", *cflags, "/c", str(src), "/Fo" + str(obj)])
        else:
            rc = run(["cl", *cppflags, "/c", str(src), "/Fo" + str(obj)])
        if rc != 0:
            return rc
    app = BIN_DIR / "demo_app.exe"
    tests_bin = TEST_DIR / "demo_tests.exe"
    rc = run(["link", "/nologo", str(OBJ_DIR / "calculator.obj"), str(OBJ_DIR / "main.obj"), "/OUT:" + str(app)])
    if rc != 0:
        return rc
    rc = run(
        [
            "link",
            "/nologo",
            str(OBJ_DIR / "calculator.obj"),
            str(OBJ_DIR / "gtest.obj"),
            str(OBJ_DIR / "test_calculator.obj"),
            "/OUT:" + str(tests_bin),
        ]
    )
    return rc


def build_with_gcc_like(cc: str):
    ensure_dirs()
    cflags = ["-Wall", "-Wextra", "-g", "-Iinclude"]
    cppflags = cflags + ["-std=c++17", "-Ithird_party/minigtest"]
    objs = [
        (Path("src/calculator.c"), OBJ_DIR / "calculator.o"),
        (Path("third_party/minigtest/gtest.cpp"), OBJ_DIR / "gtest.o"),
        (Path("tests/test_calculator.cpp"), OBJ_DIR / "test_calculator.o"),
        (Path("src/main.c"), OBJ_DIR / "main.o"),
    ]
    for src, obj in objs:
        if src.suffix == ".c":
            rc = run([cc, *cflags, "-c", str(src), "-o", str(obj)])
        else:
            rc = run([cc, *cppflags, "-c", str(src), "-o", str(obj)])
        if rc != 0:
            return rc
    app = BIN_DIR / "demo_app"
    tests_bin = TEST_DIR / "demo_tests"
    rc = run([cc, *cflags, str(OBJ_DIR / "calculator.o"), str(OBJ_DIR / "main.o"), "-o", str(app)])
    if rc != 0:
        return rc
    rc = run(
        [
            cc,
            *cppflags,
            str(OBJ_DIR / "calculator.o"),
            str(OBJ_DIR / "gtest.o"),
            str(OBJ_DIR / "test_calculator.o"),
            "-o",
            str(tests_bin),
        ]
    )
    return rc


def build():
    compiler = find_compilers()
    if not compiler:
        print("未找到可用编译器（cl/gcc/clang）。")
        return 1
    if compiler == "cl":
        return build_with_msvc()
    return build_with_gcc_like(compiler)


def run_tests():
    tests_bin = TEST_DIR / "demo_tests"
    if os.name == "nt":
        tests_bin = tests_bin.with_suffix(".exe")
    if not tests_bin.exists():
        print("测试二进制不存在，请先构建。")
        return 1
    env = os.environ.copy()
    xml_path = TEST_DIR / "report.xml"
    cmd = [str(tests_bin), f"--gtest_output=xml:{xml_path}"]
    return run(cmd, env=env)


def main():
    parser = argparse.ArgumentParser(description="Python fallback builder for demo_c_project")
    parser.add_argument("action", choices=["build", "test", "clean"], help="构建或测试")
    args = parser.parse_args()
    if args.action == "build":
        sys.exit(build())
    if args.action == "test":
        rc = build()
        if rc != 0:
            sys.exit(rc)
        sys.exit(run_tests())
    if args.action == "clean":
        if BUILD_DIR.exists():
            shutil.rmtree(BUILD_DIR)
        print("已清理 build 目录。")
        sys.exit(0)


if __name__ == "__main__":
    main()

