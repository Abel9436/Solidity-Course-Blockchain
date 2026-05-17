// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

import "./Hero.sol";

contract Mage is Hero {
    constructor() Hero(50) {}
}

contract Warrior is Hero {
    constructor() Hero(200) {}
}
