// SPDX-License-Identifier: MIT
contract Voting {
    struct Proposal {
        address target;
        bytes data;
        uint yesCount;
        uint noCount;
        bool executed;
    }

    Proposal[] public proposals;
    mapping(uint => mapping(address => bool)) public hasVoted;
    mapping(uint => mapping(address => bool)) public votedYes;
    mapping(address => bool) public members;

    event ProposalCreated(uint proposalId);
    event VoteCast(uint proposalId, address voter);
    event ProposalExecuted(uint proposalId);

    constructor(address[] memory _members) {
        members[msg.sender] = true;
        for(uint i = 0; i < _members.length; i++) {
            members[_members[i]] = true;
        }
    }

    modifier onlyMember() {
        require(members[msg.sender], "Not a member!");
        _;
    }

    function newProposal(address _target, bytes calldata _data) external onlyMember {
        proposals.push(Proposal({
            target: _target,
            data: _data,
            yesCount: 0,
            noCount: 0,
            executed: false
        }));
        emit ProposalCreated(proposals.length - 1);
    }

    function castVote(uint _proposalId, bool _supports) external onlyMember {
        Proposal storage proposal = proposals[_proposalId];
        require(!proposal.executed, "Proposal already executed!");

        if(hasVoted[_proposalId][msg.sender]) {
            // changing existing vote
            if(votedYes[_proposalId][msg.sender] && !_supports) {
                proposal.yesCount--;
                proposal.noCount++;
            } else if(!votedYes[_proposalId][msg.sender] && _supports) {
                proposal.noCount--;
                proposal.yesCount++;
            }
        } else {
            // first time voting
            if(_supports) {
                proposal.yesCount++;
            } else {
                proposal.noCount++;
            }
            hasVoted[_proposalId][msg.sender] = true;
        }

        // always update how they voted
        votedYes[_proposalId][msg.sender] = _supports;
        emit VoteCast(_proposalId, msg.sender);

        // execute if threshold reached
        if(proposal.yesCount >= 10) {
            proposal.executed = true;
            (bool s, ) = proposal.target.call(proposal.data);
            require(s, "Execution failed!");
            emit ProposalExecuted(_proposalId);
        }
    }
}