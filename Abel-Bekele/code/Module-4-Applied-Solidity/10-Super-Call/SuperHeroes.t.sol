// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "./Hero.sol";
import "./Enemy.sol";
import "./SuperHeroes.sol";

contract SuperHeroesTest is Test {
    Warrior public warrior;
    Mage public mage;
    Enemy public enemy;
    
    function setUp() public {
        warrior = new Warrior();
        mage = new Mage();
        enemy = new Enemy();
    }

    function testWarriorAttack() public {
        assertEq(warrior.energy(), 100);
        warrior.attack(enemy);
        assertEq(warrior.energy(), 90);
        assertEq(enemy.health(), 50);
    }

    function testMageAttack() public {
        assertEq(mage.energy(), 100);
        mage.attack(enemy);
        assertEq(mage.energy(), 90);
        assertEq(enemy.health(), 20);
    }
}