function myx = maxLike(TestData,paramCrack,paramPot,paramNotPot)
for i=1:length(TestData)
    Data = TestData(i,1:11);
    nlogLPot = normlike(paramPot,Data);
    nlogLNotPot = normlike(paramNotPot,Data);
    nlogLCrack = normlike(paramCrack,Data);
    if(nlogLPot>nlogLNotPot && nlogLPot>nlogLCrack)
        idxx(i)=1;
    elseif(nlogLNotPot>nlogLPot && nlogLNotPot>nlogLCrack)
        idxx(i)=2;
    elseif(nlogLCrack>nlogLNotPot && nlogLCrack>nlogLPot)
        idxx(i)=3;
    else
        idxx(i)=0;
    end
end
myx=idxx;
end