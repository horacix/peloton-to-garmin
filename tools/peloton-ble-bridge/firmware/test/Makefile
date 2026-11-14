CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -O1 -fsanitize=address,undefined

test: test_core
	./test_core

test_core: test_core.cpp ../peloton_ble_bridge/*.h
	$(CXX) $(CXXFLAGS) -o $@ test_core.cpp -lm

clean:
	rm -f test_core

.PHONY: test clean
