// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

import "./BaseContracts.sol";

contract Collectible is Ownable {
    uint public price;
    
    function markPrice(uint newPrice) external onlyOwner {
        price = newPrice;
    }
}