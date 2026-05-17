// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "./Collectible.sol";

contract CollectibleTest is Test {
    Collectible public collectible;
    address public owner = address(1);
    address public nonOwner = address(2);
    
    function setUp() public {
        vm.prank(owner);
        collectible = new Collectible();
    }
    
    function testOwnerCanMarkPrice() public {
        vm.prank(owner);
        collectible.markPrice(100);
        assertEq(collectible.price(), 100);
    }
    
    function testNonOwnerCannotMarkPrice() public {
        vm.prank(nonOwner);
        vm.expectRevert("Only owner can call this function");
        collectible.markPrice(100);
    }
}