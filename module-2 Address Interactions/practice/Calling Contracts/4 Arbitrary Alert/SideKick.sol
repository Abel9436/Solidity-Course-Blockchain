// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Sidekick {
    function relay(address hero, bytes calldata data) external {
        (bool success, ) = hero.call(data);
        require(success);
    }
}
