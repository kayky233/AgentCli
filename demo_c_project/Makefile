CC ?= gcc
CXX ?= g++
CFLAGS ?= -Wall -Wextra -g -Iinclude
CXXFLAGS ?= $(CFLAGS) -std=c++17
LDFLAGS ?=
BUILD_DIR := build
OBJ_DIR := $(BUILD_DIR)/obj
BIN_DIR := $(BUILD_DIR)/bin
TEST_DIR := $(BUILD_DIR)/tests
APP := $(BIN_DIR)/demo_app
TEST_BIN := $(TEST_DIR)/demo_tests

SRC := $(wildcard src/*.c)
OBJS := $(patsubst src/%.c,$(OBJ_DIR)/%.o,$(SRC))
TEST_SRC := $(wildcard tests/*.cpp)
TEST_OBJS := $(patsubst tests/%.cpp,$(OBJ_DIR)/%.test.o,$(TEST_SRC))
GTEST_SRC := $(wildcard third_party/minigtest/*.cpp)
GTEST_OBJS := $(patsubst third_party/minigtest/%.cpp,$(OBJ_DIR)/%.mg.o,$(GTEST_SRC))

MKDIR_P = mkdir -p

.PHONY: all build test clean

all: build

build: $(APP) $(TEST_BIN)

$(OBJ_DIR)/%.o: src/%.c include/%.h
	@$(MKDIR_P) $(OBJ_DIR)
	$(CC) $(CFLAGS) -c $< -o $@

$(APP): $(OBJS)
	@$(MKDIR_P) $(BIN_DIR)
	$(CC) $(CFLAGS) $^ -o $@ $(LDFLAGS)

$(OBJ_DIR)/%.test.o: tests/%.cpp
	@$(MKDIR_P) $(OBJ_DIR)
	$(CXX) $(CXXFLAGS) -Ithird_party/minigtest -c $< -o $@

$(OBJ_DIR)/%.mg.o: third_party/minigtest/%.cpp
	@$(MKDIR_P) $(OBJ_DIR)
	$(CXX) $(CXXFLAGS) -Ithird_party/minigtest -c $< -o $@

$(TEST_BIN): $(OBJS) $(TEST_OBJS) $(GTEST_OBJS)
	@$(MKDIR_P) $(TEST_DIR)
	$(CXX) $(CXXFLAGS) $^ -o $@ $(LDFLAGS)

test: build
	@$(MKDIR_P) $(TEST_DIR)
	@echo "Running tests..."
	@$(TEST_BIN) --gtest_output=xml:$(TEST_DIR)/report.xml

clean:
	rm -rf $(BUILD_DIR)

