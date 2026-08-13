%----------------Data Prepration for nLog--------------
StdcalcPot=std(TrainPot(:,[10 11]));
MeancalcPot=mean(TrainPot(:,[10 11] ));
StdcalcNotPot=std(TrainNotPot(:,[10 11]));
MeancalcNotPot=mean(TrainNotPot(:,[10 11]));
StdcalcCrack=std(TrainCrack(:,[10 11]));
MeancalcCrack=mean(TrainCrack(:,[10 11]));
paramNotPot = [MeancalcNotPot',StdcalcNotPot'];
paramCrack = [MeancalcCrack',StdcalcCrack'];
paramPot = [MeancalcPot',StdcalcPot'];
%TestData=[AccX1(:,1),AccY1(:,1),AccZ1(:,1),GyroX1(:,1),GyroY1(:,1),GyroZ1(:,1),LinAccX1(:,1),LinAccY1(:,1),LinAccZ1(:,1),OrientX1(:,1),OrientY1(:,1),OrientZ1(:,1),PotMag1(:,1),NormStd1(:,1)];
%TestData=[AccX(:,1),AccY(:,1),AccZ(:,1),GyroX(:,1),GyroY(:,1),GyroZ(:,1),LinAccX(:,1),LinAccY(:,1),LinAccZ(:,1),OrientX(:,1),OrientY(:,1),OrientZ(:,1),PotMag(:,1),NormStd(:,1)];
TestData=[AccX1(:,1),AccY1(:,1),AccZ1(:,1),GyroX1(:,1),GyroY1(:,1),GyroZ1(:,1),LinAccX1(:,1),LinAccY1(:,1),LinAccZ1(:,1),PotMag1(:,1),NormStd1(:,1)];
TestDatastd=[PotMag1(:,1),NormStd1(:,1)];
%maxLike(TestData,paramCrack,paramPot,paramNotPot);
%%-----------------LogLiklihoood---------------------
for i=1:length(TestData)
    Data = TestData(i,[10 11]);
    %y=(2*pi*StdcalcPot).^-0.5*exp(-0.5*((Data-MeancalcPot).^2/StdcalcPot.^2))
    nlogLPot = normlike(paramPot,Data')
    nlogLNotPot = normlike(paramNotPot,Data')
    nlogLCrack = normlike(paramCrack,Data')
    if(nlogLPot < nlogLNotPot)
        idxx(i,1)=1;
    elseif(nlogLNotPot < nlogLPot)
        idxx(i,1)=2;
    else
        idxx(i,1)=0;
    end
end
