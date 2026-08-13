[ZsNormAccmax,ZsNormAccmaxmean,ZsNormAccmaxstdev] = zscore(NormAccmax);
TrainNotPot = [ZsNormAccmax(clusterX==3,1),PotMag(clusterX==3,1),Long(clusterX==3,1),Lat(clusterX==3,1)];
TrainstdNotPot=[PotMag(clusterX==3,1),NormAccmax(clusterX==3,1)];
for i=1:length(TrainNotPot)
    type(i,:)={'NotPot'};
end
TrainCrack = [ZsNormAccmax(clusterX==1,1),PotMag(clusterX==1,1),Long(clusterX==1,1),Lat(clusterX==1,1)];
TrainstdCrack=[PotMag(clusterX==1,1),NormAccmax(clusterX==1,1)];
for i=length(TrainNotPot)+1:length(TrainNotPot)+1+length(TrainCrack)
    type(i,:)={'Crack'};
end
TrainPot = [ZsNormAccmax(clusterX==2,1),PotMag(clusterX==2,1),Long(clusterX==2,1),Lat(clusterX==2,1)];
TrainstdPot=[PotMag(clusterX==2,1),NormAccmax(clusterX==2,1)];
for i=length(TrainNotPot)+length(TrainCrack)+1:length(TrainNotPot)+length(TrainCrack)+1+length(TrainPot)
    type(i,:)={'Pot'};
end

StdcalcPot=std(TrainPot(:,[1 2]));
MeancalcPot=mean(TrainPot(:,[1 2] ));
StdcalcNotPot=std(TrainNotPot(:,[1 2]));
MeancalcNotPot=mean(TrainNotPot(:,[1 2]));
StdcalcCrack=std(TrainCrack(:,[1 2]));
MeancalcCrack=mean(TrainCrack(:,[1 2]));
paramNotPot = [MeancalcNotPot',StdcalcNotPot'];
paramCrack = [MeancalcCrack',StdcalcCrack'];
paramPot = [MeancalcPot',StdcalcPot'];

for i=1:length(AccXmax1)
    v=[AccXmax1(i,1),AccYmax1(i,1),AccZmax1(i,1)];
    NormAccmax1(i,1) = norm(v);
end
[ZsNormAccmax,ZsNormAccmaxmean,ZsNormAccmaxstdev] = zscore(NormAccmax1);

TestData=[ZsNormAccmax(:,1),PotMag1(:,1),Long1(:,1),Lat1(:,1)];
idxx=[];

for i=1:length(TestData)
    Data = TestData(i,[1 2]);
    nlogLPot = normlike(paramPot,Data');
    nlogLNotPot = normlike(paramNotPot,Data');
    nlogLCrack = normlike(paramCrack,Data');
    totval = abs(nlogLPot)+ abs(nlogLNotPot)+ abs(nlogLCrack);
    if(nlogLPot < nlogLNotPot && nlogLPot <nlogLCrack )
        idxx(i,1)=2;idxx(i,2) = nlogLPot;idxx(i,3) = nlogLNotPot;idxx(i,4) = nlogLCrack;idxx(i,5) =TestData (i,3);idxx(i,6) =TestData(i,4);
    elseif(nlogLNotPot < nlogLPot && nlogLNotPot < nlogLCrack)
        idxx(i,1)=0;idxx(i,2) =nlogLPot;idxx(i,3) = nlogLNotPot;idxx(i,4) = nlogLCrack;idxx(i,5) =TestData (i,3);idxx(i,6) =TestData(i,4);
    else
        idxx(i,1)=1;idxx(i,2) = nlogLPot;idxx(i,3) = nlogLNotPot;idxx(i,4) = nlogLCrack;idxx(i,5) =TestData (i,3);idxx(i,6) =TestData(i,4);
        
    end
end
dlmwrite('TestData.csv',idxx, 'precision', 15)
midxx=[];
for i=1:length(TestData)
    varPot=(cov(TrainPot(:,[1 2])))^2;
    varnotpot=(cov(TrainNotPot(:,[1 2])))^2;
    varcrack = (cov(TrainCrack(:,[1 2])))^2;
   
    %potindex = (1/(2*pi*(norm(varPot)^0.5)))*exp(0.5*(Data-MeancalcPot)*(varPot)^-1*(transpose(Data-MeancalcPot)));
    %crackindex = (1/(2*pi*(norm(varcrack)^0.5)))*exp(0.5*(Data-MeancalcCrack)*(varcrack)^-1*(transpose(Data-MeancalcCrack)));
    %notpotindex = (1/(2*pi*(norm(varnotpot)^0.5)))*exp(0.5*(Data-MeancalcNotPot)*(varnotpot)^-1*(transpose(Data-MeancalcNotPot)));
   Data = (TestData(i,[1 2]));
   potindex = -log(2*pi)-0.5*log(norm(varPot))-0.5*((Data-MeancalcPot)*(varPot)^-1*(transpose(Data-MeancalcPot)));
   crackindex = -log(2*pi)-0.5*log(norm(varcrack))-0.5*((Data-MeancalcCrack)*(varcrack)^-1*(transpose(Data-MeancalcCrack)));
   notpotindex = -log(2*pi)-0.5*log(norm(varnotpot))-0.5*((Data-MeancalcNotPot)*(varnotpot)^-1*(transpose(Data-MeancalcNotPot)));

        if((potindex) > (notpotindex) && (potindex) >(crackindex) )
        midxx(i,1)=2;midxx(i,2) = potindex;midxx(i,3) = notpotindex;midxx(i,4) = crackindex;midxx(i,5) =TestData (i,3);midxx(i,6) =TestData(i,4);
    elseif((notpotindex) > (potindex) && (notpotindex) > (crackindex))
        midxx(i,1)=0;midxx(i,2) =potindex;midxx(i,3) = notpotindex;midxx(i,4) = crackindex;midxx(i,5) =TestData (i,3);midxx(i,6) =TestData(i,4);
    else
        midxx(i,1)=1;midxx(i,2) = potindex;midxx(i,3) = notpotindex;midxx(i,4) = crackindex;midxx(i,5) =TestData (i,3);midxx(i,6) =TestData(i,4);
        
    end

end
dlmwrite('TestDatamax.csv',midxx, 'precision', 15)
