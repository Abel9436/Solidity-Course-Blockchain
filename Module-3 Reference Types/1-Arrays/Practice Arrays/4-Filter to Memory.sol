pragma solidity ^0.8.20;

contract Contract {
    function filterEven(uint[] memory numbers) external pure returns (uint[] memory) {
        uint count = 0;

        // first pass: count even numbers
        for (uint i = 0; i < numbers.length; i++) {
            if (numbers[i] % 2 == 0) {
                count++;
            }
        }

        // create memory array of exact size
        uint[] memory evens = new uint[](count);

        // second pass: fill array
        uint index = 0;
        for (uint i = 0; i < numbers.length; i++) {
            if (numbers[i] % 2 == 0) {
                evens[index] = numbers[i];
                index++;
            }
        }

        return evens;
    }
}