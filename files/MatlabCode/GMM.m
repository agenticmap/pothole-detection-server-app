clc
close all

for i=1:length(LinAccXmax)
    v=[LinAccXmax(i,1),LinAccYmax(i,1),LinAccZmax(i,1)];
    NormLinAccmax(i,1) = norm(v);
end
for i=1:length(AccXmin)
    v=[LinAccXmin(i,1),LinAccYmin(i,1),LinAccZmin(i,1)];
    NormAccmin(i,1) = norm(v);
end
for i=1:length(TimeInMax)
    TimeDiff(i,1) = TimeInMax(i)-TimeinMin(i);
end
% for i=1:length(SumUp)
%     v=[SumUp(i,4),SumUp(i,5),SumUp(i,6)];
%     RotatedLinAcc(i,1) = norm(v);
% end
for i=1:length(NormStd)
    ratio(i,1) = PotMag(i)/NormStd(i);
end


[ZsNormLinAccmax,ZsNormAccmaxmean,ZsNormAccmaxstdev] = zscore(NormLinAccmax);
% [ZsNormRotLinAccmax,ZsNormRotAccmaxmean,ZsNormRotAccmaxstdev] = zscore(RotatedLinAcc);
[ZsPotMag,ZsPotMagmean,ZsPotMagstdev] = zscore(PotMag);
[ZsRatio,ZsRatiomean,ZsRatiostdev] = zscore(ratio);
[ZsGbar,ZsGbaromean,ZsGbarstdev] = zscore(GbarInMax);

%ZsNormAccmax=(ZsNormAccmax);
%ZsPotMag=(ZsPotMag);
%normalized = (x-min(NormAccmax))/(max(x)-min(x));
%X=[ZsPotMag,ZsNormLinAccmax];
X=[ZsRatio,ZsGbar];
[idx,C] = kmeans(X,3,'Start','plus');
figure;
plot(X(idx==1,1),X(idx==1,2),'r.','MarkerSize',20)
hold on
plot(X(idx==2,1),X(idx==2,2),'b.','MarkerSize',20)
plot(X(idx==3,1),X(idx==3,2),'m.','MarkerSize',20)
plot(C(:,1),C(:,2),'kx',...
     'MarkerSize',15,'LineWidth',3)
legend('Cluster 1','Cluster 2','Cluster 3','Centroids',...
       'Location','NW')
title 'Cluster Assignments and Centroids'
hold off
figure
scatter(ZsRatio,ZsGbar,'filled');
figure
scatter(ratio,GbarInMax,'filled');
%PotTrain=[PotMag(idx==2,1),NormAccmax(idx==2,1),NormAccmin(idx==2,1),TimeDiff(idx==2,1),P(idx==2,[1 2 3]),Long(idx==2,1),Lat(idx==2,1)];
%CrackTrain=[PotMag(idx==3,1),NormAccmax(idx==3,1),NormAccmin(idx==3,1),TimeDiff(idx==3,1),P(idx==3,[1 2 3]),Long(idx==3,1),Lat(idx==3,1)];
%NoTrain=[PotMag(idx==3,1),NormAccmax(idx==3,1),NormAccmin(idx==3,1),TimeDiff(idx==3,1),P(idx==3,[1 2 3]),Long(idx==3,1),Lat(idx==3,1)];

k = 3;
d = 1000;

initialSigma = cat(k,cov(X(idx==1,1),X(idx==1,2)),cov(X(idx==2,1),X(idx==2,2)),cov(X(idx==3,1),X(idx==3,2)));
S = [];
S.mu=C;
%S.Sigma=initialSigma;
options = statset('MaxIter',1000);
x1 = linspace(min(X(:,1)) - 2,max(X(:,1)) + 2,d);
x2 = linspace(min(X(:,2)) - 2,max(X(:,2)) + 2,d);
gmfit = fitgmdist(X,3);
%gmfit = fitgmdist(X,k,'Options',options,'Start',start);
%gmfit = fitgmdist(X,k,'CovarianceType','full','SharedCovariance',true,'Options',options,'Start','plus');
[x1grid,x2grid] = meshgrid(x1,x2);
X0 = [x1grid(:) x2grid(:)];
clusterX = cluster(gmfit,X);
mahalDist = mahal(gmfit,X0);
figure
h1 = gscatter(X(:,1),X(:,2),clusterX);
hold on
plot(gmfit.mu(:,1),gmfit.mu(:,2),'kx','LineWidth',2,'MarkerSize',10)
hold off
P = posterior(gmfit,X);
figure
gmm = gmdistribution(gmfit.mu, gmfit.Sigma);
ezcontourf(@(x,y) pdf(gmm,[x y]));
figure
ezsurfc(@(x,y) pdf(gmm,[x y]));

PotTrain=[PotMag(clusterX==2,1),NormAccmax(clusterX==2,1),NormAccmin(clusterX==2,1),TimeDiff(clusterX==2,1),P(clusterX==2,[1 2 3]),Long(clusterX==2,1),Lat(clusterX==2,1)];
CrackTrain=[PotMag(clusterX==1,1),NormAccmax(clusterX==1,1),NormAccmin(clusterX==1,1),TimeDiff(clusterX==1,1),P(clusterX==1,[1 2 3]),Long(clusterX==1,1),Lat(clusterX==1,1)];
NoTrain=[PotMag(clusterX==3,1),NormAccmax(clusterX==3,1),NormAccmin(clusterX==3,1),TimeDiff(clusterX==3,1),P(clusterX==3,[1 2 3]),Long(clusterX==3,1),Lat(clusterX==3,1)];

dlmwrite('TrainNotPot.csv',NoTrain, 'precision', 15)
dlmwrite('TrainPot.csv',PotTrain, 'precision', 15)
dlmwrite('TrainCrack.csv',CrackTrain, 'precision', 15)

for i=1:length(AccXmin)
    v=[AccXmin(i,1),AccYmin(i,1),AccZmin(i,1)];
    NormAccmin(i,1) = norm(v);
end
% for i=1:length(TimeInMax)
%     TimeDiff(i,1) = TimeInMax(i)-TimeinMin(i);
% end
% 

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
