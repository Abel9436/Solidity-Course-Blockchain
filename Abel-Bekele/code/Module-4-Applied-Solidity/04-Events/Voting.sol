// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Voting {
    struct Proposal {
        address target;
        bytes data;
        uint yesCount;
        uint noCount;
    }
    
    event ProposalCreated(uint proposalId);
    event VoteCast(uint proposalId, address voter);
    
    Proposal[] public proposals;
    mapping(uint256 => mapping(address => bool)) public hasVoted;
    mapping(uint256 => mapping(address => bool)) public voteDirection;
    
    function newProposal(address target, bytes calldata data) external {
        Proposal memory newProposal = Proposal({
            target: target,
            data: data,
            yesCount: 0,
            noCount: 0
        });
        proposals.push(newProposal);
        emit ProposalCreated(proposals.length - 1);
    }
    
    function castVote(uint proposalId, bool support) external {
        Proposal storage proposal = proposals[proposalId];
        
        if (hasVoted[proposalId][msg.sender]) {
            bool previousVote = voteDirection[proposalId][msg.sender];
            if (previousVote != support) {
                if (support) {
                    proposal.noCount--;
                    proposal.yesCount++;
                } else {
                    proposal.yesCount--;
                    proposal.noCount++;
                }
                voteDirection[proposalId][msg.sender] = support;
            }
        } else {
            if (support) {
                proposal.yesCount++;
            } else {
                proposal.noCount++;
            }
            hasVoted[proposalId][msg.sender] = true;
            voteDirection[proposalId][msg.sender] = support;
        }
        
        emit VoteCast(proposalId, msg.sender);
    }
}