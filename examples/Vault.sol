// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
    }

    function quote(uint256 assets, uint256 rate) external pure returns (uint256) {
        return assets / rate;
    }
}
