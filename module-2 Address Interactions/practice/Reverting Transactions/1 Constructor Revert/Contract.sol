// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;
contract Contract {
    address public owner;

    constructor() payable {
        owner = msg.sender;
    }

    receive() external payable {}

    function withdraw() public {
        require(msg.sender == owner, "Not owner");

        (bool success, ) = owner.call{value: address(this).balance}("");
        require(success);
    }
}
