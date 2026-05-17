# 🔗 Solidity & Blockchain Development Course

Welcome to the **Solidity and Blockchain Development Course** repository! This repository serves as a centralized hub for all practical exercises, smart contract implementations, testing suites, and completion certifications completed by our development group. 

Each member of the group has worked on a dedicated branch, containing structured folders for every Solidity and smart contract design pattern explored during the course—ranging from core syntax to complex multi-inheritance, voting mechanics, custom escrow protocols, and contract-to-contract communication.

---

## 👥 Group Members & Branch Registry

To view a group member's specific codebase, deliverables, and course certifications, you can redirect directly to their branch or designated folder using the directory cards below:

| Group Member | Student UGR ID | Active Development Branch | Quick Branch Redirect | Direct Workspace Link |
| :--- | :---: | :--- | :---: | :---: |
| **Abel Bekele** | `ugr/25421/14` | [`Abel-Bekele-UGR/25421/14`](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Abel-Bekele-UGR/25421/14) | [**Explore Branch ↗**](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Abel-Bekele-UGR/25421/14) | [**Abel's Directory 📁**](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Abel-Bekele-UGR/25421/14/Abel-Bekele) |
| **Milkessa Girma** | `ugr/25294-14` | [`Milkessa-Girma-UGR/25294/14`](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Milkessa-Girma-UGR-25294-14) | [**Explore Branch ↗**](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Milkessa-Girma-UGR-25294-14) | [**Milkessa's Directory 📁**](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Milkessa-Girma-UGR-25294-14) |
**Yaikob Wasihun** | `ugr/25294-14` | [`Yaikob-Wasihun-UGR/25294/14`](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Yaikob-Wasihun-UGR/25294/14) | [**Explore Branch ↗**](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Yaikob-Wasihun-UGR/25294/14) | [**Yaikob's Directory 📁**](https://github.com/Abel9436/Solidity-Course-Blockchain/tree/Yaikob-Wasihun-UGR/25294/14) |

---

## 📚 Curriculum & Course Modules Completed

Our team has completed 4 rigorous practical modules. Each module showcases a core vertical of smart contract engineering in Solidity:

### 🔹 Module 1: Solidity Introduction
A deep dive into foundational language grammar, memory layout basics, and math operations:
- **Core Types**: Unsigned integers (`uint`), Signed integers (`int`), Booleans (`bool`), and Strings (`string`).
- **Function Mutability**: Understanding `pure` and `view` vs write-enabled methods.
- **Advanced Basic Concepts**: Function overloading, double overload resolving, and using `Console_Log` for contract debugging.

### 🔹 Module 2: Address Interactions
Explores Ether transfers, custom transaction rules, secure custody, and raw calldata interfaces:
- **Sending Ether**: Storing contract owners, receiving native gas tokens via `receive()`, tipping mechanics, and secure `selfdestruct` payouts.
- **Reverting Transactions**: Restricting access via modifiers (`onlyOwner`), enforcing assertions, and reverting calls with custom error states.
- **Escrow Protocol**: A multi-stage Escrow contract containing:
  - Depositing funds in a constructor.
  - Verification & approval by designated arbiters.
  - Secure release of locked assets to primary beneficiaries.
  - Architectural event tracking.
- **Calling Contracts**: Utilizing Solidity calldata signature interfaces, fallback methods, and dynamic raw calling structures.

### 🔹 Module 3: Reference Types
Focuses on dynamic storage allocation, reference pointer behavior, and memory arrays:
- **Arrays**: Fixed vs Dynamic-length storage/memory array sorting, sum filters, and data manipulation.
- **Structs**: Packaging complex variables (e.g., custom Vote proposals and Member data structures).
- **Mappings**: Managing constant-time lookups, building nesting mappings (`mapping(address => mapping(address => bool))`), and tracking structural members.

### 🔹 Module 4: Applied Solidity
Implementation of complex, production-grade systems and object-oriented paradigms:
- **Decentralized Voting System**: Fully functional ballot contracts including proposal storage, cast counting, multiple vote protection, event logging, registry-based member checks, and final transaction execution.
- **Object-Oriented Solidity**:
  - **Inheritance & Constructors**: Overriding parent functions, and passing initialization arguments to base contracts.
  - **Super Calls & Virtual Methods**: Overriding base behavior using `virtual` and `override` syntax.
  - **Multi-Inheritance Patterns**: Structuring contracts inheriting from multiple abstract files (e.g., `Ownable`, `Collectible`, and `Transferable`).

---

## 📂 Repository Structures

### 📂 Abel Bekele's Branch Structure (`Abel-Bekele-UGR/25421/14`)
Abel's branch contains a dedicated `Abel-Bekele/` folder, neatly separating source code, course materials, and proof-of-completion deliverables:

```text
Abel-Bekele/
├── code/
│   ├── module-1-Solidity-Introduction/      # Core syntax & type exercises
│   ├── Module-2-Address-Interactions/       # Escrows, Sending Ether & Calldata
│   ├── Module-3 Reference Types/            # Arrays, Structs, and Mapping systems
│   └── Module-4-Applied-Solidity/           # Voting systems & Multiple Inheritance
├── materials/
│   └── module-1-Solidity-Introduction/      # Distributed course handouts & reading guides
└── screenshots/
    ├── module-1-Solidity-Introduction/      # Code execution proof
    ├── Module-3 Reference Types/            # Verification & course progress logs
    └── Module-4-Applied-Solidity/           # Passing tests & terminal snapshots
```

### 📂 Milkessa Girma's Branch Structure (`Milkessa-Girma-UGR-25294-14`)
Milkessa's branch structures materials at the root level, making them instantly accessible from the branch landing page:

```text
├── Module-1 Solidity-Introduction/          # Complete introductions & basics
├── module-2 Address Interactions/           # Escrow mechanics & calling contracts
├── Module-3 Reference Types/                # Mappings, Structs, Arrays sum practices
├── Module-4 Applied Solidity/               # Voting algorithms & inheritance trees
├── course-materials/                        # References, syllabi, & study guides
└── Course Completion/                       # Proof of final completion certifications
```

---

## 🛠 Tech Stack & Tools Used

- **Language**: Solidity (`^0.8.0` / `^0.8.20`)
- **Testing Frameworks**: Foundry & Hardhat (for `.t.sol` contracts and validation test suites)
- **Local Compiler**: `solc`
- **Environment**: Visual Studio Code with Solidity extension support

---

## 📜 How to Review the Projects

1. **Clone the repository:**
   ```bash
   git clone https://github.com/Abel9436/Solidity-Course-Blockchain.git
   cd Solidity-Course-Blockchain
   ```

2. **Checkout Abel Bekele's Workspace:**
   ```bash
   git checkout Abel-Bekele-UGR/25421/14
   # Navigate to Abel-Bekele folder to run tests
   cd Abel-Bekele
   ```

3. **Checkout Milkessa Girma's Workspace:**
   ```bash
   git checkout Milkessa-Girma-UGR-25294-14
   # Explore directly in the root directory
   ```

---

