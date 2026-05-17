// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

import "./Ownable.sol";

contract Transferable is Ownable {
    function transfer(address newOwner) external onlyOwner {
        require(newOwner != address(0), "New owner cannot be zero address");
        owner = newOwner;
    }
}