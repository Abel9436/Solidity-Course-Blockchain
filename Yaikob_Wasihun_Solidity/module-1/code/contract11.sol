// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Contract {
    function double(uint _x) external pure returns(uint result) {
        result = _x * 2;
    }
}