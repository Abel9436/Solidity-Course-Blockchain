// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Voting {
     struct Proposal {
        address target;
        bytes data;
        uint yesCount;
        uint noCount;
    }

    Proposal[] public proposals;

    function newProposal(address target, bytes calldata data) external {
        proposals.push(
            Proposal({
                target: target,
                data: data,
                yesCount: 0,
                noCount: 0
            })
        );
    }

   function castVote(uint proposalId, bool support) external {
    Proposal storage proposal = proposals[proposalId];

    if (support) {
        proposal.yesCount++;
    } else {
        proposal.noCount++;
    }
}
}
