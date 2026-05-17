// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract Contract {
    enum Foods { Pizza, Burger, Pasta, Sushi }

    Foods public food1 = Foods.Pizza;
    Foods public food2 = Foods.Burger;
    Foods public food3 = Foods.Pasta;
    Foods public food4 = Foods.Sushi;
}