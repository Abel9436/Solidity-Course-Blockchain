// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Voting {
   struct Proposal {
        address target;
        bytes data;
        uint yesCount;
        uint noCount;
        bool executed;

        mapping(address => bool) hasVoted;
        mapping(address => bool) voteChoice;
    }

    Proposal[] public proposals;

    event ProposalCreated(uint proposalId);
    event VoteCast(uint proposalId, address voter);

    mapping(address => bool) public members;

    constructor(address[] memory initialMembers) {
        members[msg.sender] = true;

        for (uint i = 0; i < initialMembers.length; i++) {
            members[initialMembers[i]] = true;
        }
    }

    function newProposal(address target, bytes calldata data) external {
        require(members[msg.sender], "Not a member");

        Proposal storage p = proposals.push();
        p.target = target;
        p.data = data;

        emit ProposalCreated(proposals.length - 1);
    }

    function castVote(uint proposalId, bool support) external {
        require(members[msg.sender], "Not a member");

        Proposal storage proposal = proposals[proposalId];
        require(!proposal.executed, "Already executed");

        if (proposal.hasVoted[msg.sender]) {
            bool previous = proposal.voteChoice[msg.sender];

            if (previous) {
                proposal.yesCount--;
            } else {
                proposal.noCount--;
            }
        }

        proposal.hasVoted[msg.sender] = true;
        proposal.voteChoice[msg.sender] = support;

        if (support) {
            proposal.yesCount++;
        } else {
            proposal.noCount++;
        }

        emit VoteCast(proposalId, msg.sender);

        // Execute when threshold reached
        if (!proposal.executed && proposal.yesCount >= 10) {
            proposal.executed = true;

            (bool success, ) = proposal.target.call(proposal.data);
            require(success, "Execution failed");
        }
    }
}
