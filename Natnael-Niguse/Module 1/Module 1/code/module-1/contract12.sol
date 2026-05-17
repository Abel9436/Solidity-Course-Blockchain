// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Contract {
    function double(uint _x) public pure returns(uint) {
        return _x * 2;
    }

    function double(uint _x, uint _y) external pure returns(uint, uint) {
        return (double(_x), double(_y));
    }
}