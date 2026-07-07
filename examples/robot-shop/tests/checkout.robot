*** Settings ***
Documentation     Checkout flows of the fictional web shop (neutral demo corpus).

*** Variables ***
${STAGE}          dev

*** Test Cases ***
Guest Checkout Completes
    [Tags]    Checkout    Smoke
    Log    guest completes checkout
    Should Be True    ${True}

Registered Checkout Completes
    [Tags]    Checkout    Regression
    Log    registered user completes checkout
    Should Be True    ${True}

Slow Warehouse Sync Finishes
    [Tags]    Checkout    Regression    SlowLane
    [Documentation]    Deliberately slow: keeps the run in RUNNING long enough
    ...    to observe live per-item status (DESIGN.md §6.2a listener path).
    Sleep    3s
    Log    warehouse sync finished

Slow Invoice Batch Finishes
    [Tags]    Checkout    Regression    SlowLane
    Sleep    6s
    Log    invoice batch finished

Stage Variable Reaches The Test
    [Tags]    Checkout    VarCheck
    [Documentation]    Passes only when the run spec's runner variables
    ...    override the suite default (STAGE=dev → test).
    Should Be Equal    ${STAGE}    test
