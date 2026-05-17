// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

abstract contract Hero {
    enum AttackTypes { Brawl, Spell }
    
    uint public health;
    
    constructor(uint _health) {
        health = _health;
    }
    
    function takeDamage(uint damage) public {
        health -= damage;
    }
    
    function attack(Enemy enemy) public virtual;
}